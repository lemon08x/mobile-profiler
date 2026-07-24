from __future__ import annotations

import copy
import html
import json
import math
import statistics
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .features import CAPTURE_FEATURES, PLATFORM_FEATURE_PRESENTATION
from .messages import localize_collection_warning
from .models import APP_NAME


SOURCE_KIND_LABELS = {
    "measured": "实测",
    "measured counters": "实测计数器",
    "measured context": "实测上下文",
    "counter": "计数器",
    "driver": "驱动",
    "model": "模型",
    "context": "上下文",
    "diagnostic score": "诊断分数",
    "measured low-rate telemetry": "低频实测遥测",
    "event context": "事件上下文",
    "interference context, not net overhead": "干扰上界，非净开销",
    "system compositor context": "系统合成上下文",
    "battery flow; consumption-valid only while discharging": "电池流量；仅放电区间可用于耗电",
    "low": "正常",
    "medium": "中等",
    "high": "高",
    "info": "信息",
}
CONFIDENCE_LABELS = {"low": "低", "medium": "中", "high": "高"}
CATEGORY_LABELS = {
    "application": "应用",
    "android_system": "Android 系统",
    "native_system": "系统原生服务",
    "vendor_service": "厂商服务",
    "kernel": "内核任务",
    "tooling": "采集工具",
    "dex_optimization": "DEX 优化",
    "package_management": "安装与包管理",
    "system_update": "系统更新",
    "other": "其他",
}
THERMAL_STATUS_LABELS = {
    "none": "正常",
    "light": "轻度",
    "moderate": "中度",
    "severe": "严重",
    "critical": "危急",
    "emergency": "紧急",
    "shutdown": "关机",
    "not_applicable": "不适用",
    "unknown": "未知",
}
ACTIVITY_KIND_LABELS = {
    "dex_optimization": "DEX 优化",
    "package_management": "安装与包管理",
    "system_update": "系统更新",
    "runtime_gc": "ART / GC",
    "kernel_workqueue": "kworker",
    "kernel_workqueue_storage": "kworker · 存储",
    "kernel_workqueue_display": "kworker · 显示",
    "kernel_workqueue_network": "kworker · 网络",
    "kernel_workqueue_memory": "kworker · 内存",
    "kernel_workqueue_power_thermal": "kworker · 电源 / 热控",
    "kernel_rcu": "RCU",
    "kernel_softirq": "softirq",
    "kernel_irq": "IRQ",
    "kernel_storage": "内核存储",
    "kernel_memory": "内存回收 / 压缩",
    "kernel_scheduler": "内核调度",
    "kernel_power_thermal": "电源 / 热控",
    "display_composition": "显示合成",
}
INTERFERENCE_LABELS = {"low": "低", "medium": "中", "high": "高", "unknown": "未知"}
METRIC_LABELS = {
    "Whole-device raw SystemLoad power": "iOS 整机原始 SystemLoad 通道",
    "Whole-device battery power": "iOS 整机原始 SystemLoad 通道",
    "Battery current and voltage": "电池电流与电压",
    "CPU and process activity": "CPU 与进程活动",
    "Relative process power score": "进程相对功耗分数",
    "System processes and collector overhead": "系统进程与观察者相关进程 CPU",
    "Battery temperature": "电池温度",
    "Battery current, voltage and temperature": "电池电流、电压与温度",
    "CPU utilization": "CPU 利用率",
    "CPU frequency": "CPU 频率",
    "Display refresh rate": "屏幕刷新率",
    "Target process resources": "目标进程资源",
    "Delivered touch interactions": "系统已分发触控事件",
    "Foreground application and screen state": "前台应用与屏幕状态",
    "System processes": "系统进程",
    "Thermal sensors": "热传感器",
    "Power and scheduler context": "电源与调度上下文",
    "Battery current": "电池电流",
    "Battery voltage": "电池电压",
    "CPU utilization/frequency": "CPU 利用率 / 频率",
    "CPU frequency impact": "CPU 频率影响",
    "Memory frequency pressure": "内存频率",
    "GPU activity": "GPU 活动",
    "Component/app attribution": "组件 / 应用归因",
    "Foreground application": "前台应用",
    "Test phases and actions": "测试阶段与动作",
    "Per-test power and system interference": "分测试项功耗与系统干扰",
    "System processes and hot threads": "系统进程与热点线程",
    "Thermal severity, sensors and cooling devices": "热级别、传感器与冷却设备",
    "cpuset, process state and ADPF hints": "cpuset、进程状态与 ADPF Hint",
    "Frame rate, 1% Low and frame latency": "帧率、1% Low 与帧延迟",
    "Render pipeline stages": "渲染链路阶段",
    "CPU, GPU and memory frequency context": "CPU / GPU / 内存频率上下文",
    "Render and compositor thread activity": "渲染与合成线程活动",
    "Scheduler and thermal context": "调度与热状态上下文",
    "Scheduler context": "调度上下文",
    "Thermal context": "热状态上下文",
    "Whole-device power recording": "电池侧功率记录",
    "Brightness thermal limiting": "屏幕热降亮",
    "iOS CPU and GPU performance context": "iOS CPU / GPU 性能上下文",
    "Foreground application state": "前台应用状态",
    "Observer-related process CPU upper bound": "观察者相关进程 CPU 上界",
    "HarmonyOS application frame pacing": "HarmonyOS 应用帧节奏",
    "HarmonyOS compositor cadence context": "HarmonyOS 系统合成节奏上下文",
    "HarmonyOS CPU/GPU/DDR and thermal context": "HarmonyOS CPU / GPU / DDR 与温度上下文",
}
SOURCE_LABELS = {
    "iOS DiagnosticsService PowerTelemetryData.SystemLoad": "iOS DiagnosticsService · PowerTelemetryData.SystemLoad",
    "iOS DiagnosticsService battery properties": "iOS DiagnosticsService · 电池属性",
    "iOS DVT sysmontap": "iOS DVT sysmontap",
    "iOS DVT sysmontap powerScore": "iOS DVT sysmontap · powerScore（相对分数）",
    "iOS DVT Graphics utilization": "iOS DVT Graphics 利用率",
    "iOS DVT application-state notifications": "iOS DVT 应用状态通知",
    "Imported timestamped logs aligned to device uptime": "按设备 uptime 对齐的外部时间戳日志",
    "iOS DVT sysmontap process snapshots": "iOS DVT sysmontap 进程快照",
    "iOS DiagnosticsService battery temperature": "iOS DiagnosticsService · 电池温度",
    "BatteryService / fuel gauge": "BatteryService / 电量计",
    "BatteryService state": "BatteryService 状态",
    "Power Profile estimate": "Power Profile 模型估算",
    "BatteryStats": "BatteryStats 模型",
    "BatteryStats model": "BatteryStats 模型",
    "Power Profile brightness estimate": "Power Profile 亮度估算",
    "OEM devfreq/KGSL when readable; dumpsys gpu UID work and memory otherwise": "可读时使用 OEM devfreq/KGSL，否则使用 dumpsys gpu UID 工作和内存快照",
    "Android /proc/stat utilization deltas": "Android /proc/stat 利用率增量",
    "Android cpufreq core-group counters": "Android cpufreq 核心组频率计数器",
    "Android DisplayManager context sampler": "Android DisplayManager 上下文采样",
    "Readable Android KGSL/OEM GPU frequency and load counters": "可读的 Android KGSL / OEM GPU 频率与负载计数器",
    "Readable DRAM/DMC/MIF devfreq clock": "可读的 DRAM / DMC / MIF devfreq 时钟",
    "ActivityManager context sampler": "ActivityManager 上下文采样",
    "Imported timestamped logs aligned by /proc/uptime": "按 /proc/uptime 对齐的外部时间戳日志",
    "Whole-device telemetry + aligned process/thread/thermal snapshots": "整机遥测 + 对齐后的进程 / 线程 / 热状态快照",
    "Periodic toybox top/ps snapshots": "周期性 toybox top / ps 快照",
    "Android ThermalService / thermal HAL": "Android ThermalService / Thermal HAL",
    "cgroup files + ActivityManager + performance_hint": "cgroup 文件 + ActivityManager + performance_hint",
    "HarmonyOS BatteryService via hidumper": "HarmonyOS BatteryService · hidumper",
    "HarmonyOS /proc/stat via persistent HDC shell": "HarmonyOS /proc/stat · 持久 HDC shell",
    "HarmonyOS hidumper --cpufreq": "HarmonyOS hidumper --cpufreq",
    "HarmonyOS AbilityManager + PowerManagerService": "HarmonyOS AbilityManager + PowerManagerService",
    "HarmonyOS top + ps over HDC": "HarmonyOS top + ps · HDC",
    "HarmonyOS ThermalService via hidumper": "HarmonyOS ThermalService · hidumper",
    "HarmonyOS PowerManagerService + cpufreq capability snapshots": "HarmonyOS PowerManagerService + cpufreq 能力快照",
    "HarmonyOS /proc/stat + hidumper --cpufreq": "HarmonyOS /proc/stat + hidumper --cpufreq",
    "HarmonyOS SmartPerf SP_daemon GPU fields": "HarmonyOS SmartPerf · GPU 字段",
    "HarmonyOS SmartPerf SP_daemon DDR fields": "HarmonyOS SmartPerf · DDR 字段",
    "HarmonyOS SmartPerf SP_daemon temperature fields": "HarmonyOS SmartPerf · 温度字段",
    "HarmonyOS SmartPerf SP_daemon target process fields": "HarmonyOS SmartPerf · 目标进程字段",
    "HarmonyOS SmartPerf SP_daemon current and voltage fields": "HarmonyOS SmartPerf · 电流与电压字段",
    "HarmonyOS AbilityManager + WindowManager": "HarmonyOS AbilityManager + WindowManager",
    "HarmonyOS RenderService screen refresh-rate counters": "HarmonyOS RenderService · 屏幕刷新率计数器",
    "HarmonyOS MultimodalInput delivered touch events": "HarmonyOS MultimodalInput · 系统已分发触控事件",
    "Imported timestamped logs aligned to HarmonyOS device realtime": "按 HarmonyOS 设备实时时钟对齐的外部日志",
    "Android SurfaceFlinger foreground application-layer present timestamps with gfxinfo fallback and detailed framestats": "Android SurfaceFlinger 前台应用层呈现时间戳 + gfxinfo / framestats 回退",
    "Platform utilization, cpufreq and readable devfreq counters": "平台利用率、cpufreq 与可读 devfreq 计数器",
    "Periodic toybox top/ps thread snapshots": "周期性 toybox top / ps 线程快照",
    "Battery current and voltage telemetry": "电池电流与电压遥测",
    "DisplayManager BrightnessThermalClamper + Thermal HAL lcd-backlight": "DisplayManager 热亮度限制 + Thermal HAL lcd-backlight",
    "iOS DVT sysmontap + Graphics utilization when events are observed": "iOS DVT sysmontap + 实际收到的 Graphics 事件",
    "iOS DVT application-state notifications when observed": "iOS DVT 实际收到的应用状态通知",
    "sysmond + DTServiceHub + remotepairingdeviced concurrent CPU": "sysmond + DTServiceHub + remotepairingdeviced 同期 CPU",
    "HarmonyOS SmartPerf SP_daemon target-foreground FPS and raw frame jitter": "HarmonyOS SmartPerf · 目标前台 FPS 与原始 jitter",
    "HarmonyOS RenderService fresh active-screen compositor timestamps": "HarmonyOS RenderService · 亮屏新鲜合成时间戳",
    "SmartPerf fields when verified; otherwise HDC /proc/stat, cpufreq and ThermalService": "验证可用时使用 SmartPerf；否则使用 HDC /proc/stat、cpufreq 与 ThermalService",
    "HarmonyOS BatteryService current and voltage telemetry": "HarmonyOS BatteryService 电流与电压遥测",
    "HarmonyOS DisplayPowerManagerService + RenderService + ThermalService": "HarmonyOS DisplayPowerManagerService + RenderService + ThermalService",
    "HarmonyOS DisplayPowerManagerService Brightness/DeviceBrightness/Discount + ThermalService": "HarmonyOS 逻辑/设备亮度、显示折扣 + ThermalService",
    "iOS AppleARMBacklight user brightness/rawBrightness/BrightnessMilliNits": "iOS AppleARMBacklight 用户亮度 / 原始背光 / 实际毫尼特",
}
COMPONENT_LABELS = {
    "screen": "屏幕",
    "cpu": "CPU",
    "audio": "音频",
    "wifi": "Wi-Fi",
    "wakelock": "Wakelock",
    "bluetooth": "蓝牙",
    "mobile_radio": "蜂窝网络",
}
CLUSTER_LABELS = {
    "Little": "小核",
    "Middle": "中核",
    "Big": "大核",
    "Performance": "性能核",
    "Prime": "超大核",
    "CPU": "CPU",
}


def _display_label(value: object, labels: Dict[str, str]) -> str:
    text = str(value or "")
    return labels.get(text, text or "—")


def _escape(value: object) -> str:
    return html.escape(str(value))


def _number(value: object, digits: int = 1, fallback: str = "—") -> str:
    if not isinstance(value, (int, float)):
        return fallback
    return f"{float(value):.{digits}f}"


def _byte_size(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    size = float(value)
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.2f} GiB"
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.1f} MiB"
    if size >= 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size:.0f} B"


def _json_for_script(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")


def _summary_cards(summary: Dict[str, object]) -> str:
    power_sources = summary.get("power_sources")
    power_sources = power_sources if isinstance(power_sources, list) else []
    has_system_load = any(
        str(item) == "ios_power_telemetry_system_load" for item in power_sources
    )
    valid_consumption = bool(summary.get("power_valid_for_consumption"))
    average_power = summary.get("average_power_mw")
    p95_power = summary.get("p95_power_mw")
    observed_average = summary.get("observed_power_average_mw")
    if valid_consumption and isinstance(p95_power, (int, float)):
        power_context = f"有效放电区间 P95 {float(p95_power) / 1000.0:.3f} W"
    elif isinstance(observed_average, (int, float)):
        source_label = (
            "iOS SystemLoad 原始均值"
            if has_system_load
            else "原始功率通道均值"
        )
        power_context = (
            f"{source_label} {float(observed_average) / 1000.0:.3f} W；"
            "无有效放电区间，不作耗电结论"
        )
    else:
        power_context = "无有效放电区间，不生成平均耗电结论"
    if any(str(item).startswith("ios_") for item in power_sources):
        maximum_age = summary.get("maximum_power_sample_age_s")
        power_context += (
            f" · 最大样本年龄 {_number(maximum_age)} s"
            if isinstance(maximum_age, (int, float))
            else " · iOS SystemLoad 低频刷新"
        )
    maximum_cpu = summary.get("maximum_cpu_pct")
    cpu_context = (
        f"峰值 {float(maximum_cpu):.1f}%"
        if isinstance(maximum_cpu, (int, float))
        else "本次没有有效 CPU 负载样本"
    )
    collector_cpu = summary.get("average_collector_cpu_pct")
    if isinstance(collector_cpu, (int, float)):
        cpu_context += f" · 观察者相关进程上界 {float(collector_cpu):.1f}%"
    signed_current = summary.get("average_signed_current_ma")
    current_context = (
        "充电电流正幅值"
        if isinstance(signed_current, (int, float)) and float(signed_current) > 0
        else "放电电流正幅值"
        if isinstance(signed_current, (int, float)) and float(signed_current) < 0
        else "电流正幅值"
    )
    cards = [
        (
            "有效平均耗电功率",
            (
                f"{float(average_power) / 1000.0:.3f}"
                if valid_consumption and isinstance(average_power, (int, float))
                else "—"
            ),
            "W",
            power_context,
            "measured",
        ),
        (
            "电池电流",
            _number(summary.get("average_current_ma"), 1),
            "mA",
            current_context,
            "measured",
        ),
        (
            "电池电压",
            (
                f"{float(summary['average_voltage_mv']) / 1000.0:.3f}"
                if isinstance(summary.get("average_voltage_mv"), (int, float))
                else "—"
            ),
            "V",
            (
                f"有效放电区间 {float(summary['energy_per_minute_mwh']):.2f} mWh/min"
                if isinstance(summary.get("energy_per_minute_mwh"), (int, float))
                else "原始电池电压通道"
            ),
            "measured",
        ),
        (
            "CPU 利用率",
            _number(summary.get("average_cpu_pct"), 1),
            "%",
            cpu_context,
            "counter",
        ),
    ]
    return "".join(
        '<article class="metric-card">'
        f'<div class="metric-top"><span>{_escape(label)}</span><span class="source-tag {kind}">{_escape(_display_label(kind, SOURCE_KIND_LABELS))}</span></div>'
        f'<div class="metric-value">{_escape(value)} <small>{_escape(unit)}</small></div>'
        f'<div class="metric-context">{_escape(context)}</div>'
        "</article>"
        for label, value, unit, context, kind in cards
    )


def _performance_cards(analysis: Dict[str, object]) -> str:
    performance = analysis.get("performance", {})
    performance = performance if isinstance(performance, dict) else {}
    frame_timeline = performance.get("frame_rate_timeline", [])
    frame_timeline = frame_timeline if isinstance(frame_timeline, list) else []

    def shown(value: object, digits: int = 1) -> str:
        return _number(value, digits) if isinstance(value, (int, float)) else "—"

    refresh = performance.get("current_refresh_rate_hz")
    peak = performance.get("peak_refresh_rate_hz")
    fps = performance.get("sampled_frame_rate_fps")
    if not isinstance(fps, (int, float)):
        fps = performance.get("sampled_compositor_fps")
    frame_p95 = performance.get("frame_metric_p95_ms")
    if not isinstance(frame_p95, (int, float)):
        frame_p95 = performance.get("frame_interval_p95_ms")
    missed = performance.get("frame_issue_pct")
    if not isinstance(missed, (int, float)):
        missed = performance.get("missed_vsync_interval_pct")
    frame_rate_label = str(performance.get("frame_rate_label") or "合成器抽样 FPS")
    frame_rate_unit = str(performance.get("frame_rate_unit") or "FPS")
    frame_metric_label = str(performance.get("frame_metric_label") or "最差抽样 P95")
    frame_issue_label = str(performance.get("frame_issue_label") or "跨越刷新槽位")
    frame_unavailable_reason = str(performance.get("frame_unavailable_reason") or "")
    one_percent_low = performance.get("one_percent_low_fps")
    one_percent_low_source = str(performance.get("one_percent_low_source") or "")
    detailed_one_percent_low = (
        isinstance(one_percent_low, (int, float))
        and (
            any(
                isinstance(item, dict)
                and isinstance(item.get("frame_intervals_ms"), list)
                and bool(item.get("frame_intervals_ms"))
                for item in frame_timeline
            )
            or (
                any(
                    token in one_percent_low_source.lower()
                    for token in ("slowest 1%", "frame-time histogram", "frame-jitter")
                )
                and "sampled-window" not in one_percent_low_source.lower()
                and "counter-window" not in one_percent_low_source.lower()
            )
        )
    )
    if not detailed_one_percent_low:
        one_percent_low = None
        one_percent_low_source = "仅有采样窗口 FPS 时不生成标准 1% Low"
    cards = [
        (
            "当前刷新率",
            shown(refresh, 0),
            "Hz",
            f"设备最高 {shown(peak, 0)} Hz" if isinstance(peak, (int, float)) else "显示档位上下文",
            "counter",
        ),
        (
            frame_rate_label,
            shown(fps, 1),
            frame_rate_unit,
            (
                f"累计 {int(performance.get('frame_sample_count') or 0)} 帧"
                if isinstance(fps, (int, float))
                else frame_unavailable_reason or "当前会话没有可用帧计数"
            ),
            "counter",
        ),
        (
            frame_metric_label,
            shown(frame_p95, 2),
            "ms",
            (
                f"{shown(missed, 2)}% {frame_issue_label}"
                if isinstance(frame_p95, (int, float))
                else frame_unavailable_reason or "当前会话没有可用帧耗时"
            ),
            "counter",
        ),
        (
            "1% Low",
            shown(one_percent_low, 1),
            frame_rate_unit,
            (
                one_percent_low_source
                if isinstance(one_percent_low, (int, float))
                else frame_unavailable_reason or "当前会话没有足够的帧耗时样本"
            ),
            "counter",
        ),
    ]
    return "".join(
        '<article class="metric-card">'
        f'<div class="metric-top"><span>{_escape(label)}</span><span class="source-tag {kind}">{_escape(_display_label(kind, SOURCE_KIND_LABELS))}</span></div>'
        f'<div class="metric-value">{_escape(value)} <small>{_escape(unit)}</small></div>'
        f'<div class="metric-context">{_escape(context)}</div>'
        "</article>"
        for label, value, unit, context, kind in cards
    )


def _refresh_residency_rows(analysis: Dict[str, object]) -> str:
    performance = analysis.get("performance", {})
    performance = performance if isinstance(performance, dict) else {}
    rows = []
    for item in performance.get("refresh_residency", []) if isinstance(performance.get("refresh_residency"), list) else []:
        share = max(0.0, min(100.0, float(item.get("share_pct") or 0.0)))
        rows.append(
            '<div class="residency-row">'
            f'<div><strong>{_number(item.get("refresh_rate_hz"), 0)} Hz</strong><span>{_number(item.get("estimated_duration_s"), 1)} s</span></div>'
            '<div class="stacked-bar" role="img" '
            f'aria-label="{_number(item.get("refresh_rate_hz"), 0)} Hz 驻留 {share:.1f}%">'
            f'<span class="band-balanced" style="width:{share:.3f}%"></span>'
            "</div>"
            f'<div class="residency-values"><span>{share:.1f}%</span></div>'
            "</div>"
        )
    return "".join(rows) or '<div class="availability-note"><strong>刷新档位驻留不可用</strong><span>当前平台没有提供可计算的刷新档位计数变化。</span></div>'


def _performance_context_rows(analysis: Dict[str, object]) -> str:
    platform = str(analysis.get("platform") or "android").strip().lower()
    performance = analysis.get("performance", {})
    performance = performance if isinstance(performance, dict) else {}
    thermal = analysis.get("thermal", {})
    thermal = thermal if isinstance(thermal, dict) else {}
    hottest = thermal.get("hottest_sensor")
    hottest = hottest if isinstance(hottest, dict) else {}
    width = performance.get("display_width_px")
    height = performance.get("display_height_px")
    resolution = (
        f"{int(width)} × {int(height)}"
        if isinstance(width, (int, float)) and isinstance(height, (int, float))
        else "—"
    )
    supported = performance.get("supported_refresh_rates_hz", [])
    supported_text = (
        " / ".join(f"{float(value):g}" for value in supported if isinstance(value, (int, float))) + " Hz"
        if isinstance(supported, list) and supported
        else "—"
    )
    hottest_text = (
        f"{_number(hottest.get('maximum_value', hottest.get('maximum_c')), 1)} {hottest.get('unit') or '°C'} · {hottest.get('name') or 'sensor'}"
        if hottest
        else "—"
    )
    render_width = performance.get("render_width_px")
    render_height = performance.get("render_height_px")
    render_resolution = (
        f"{int(render_width)} × {int(render_height)}"
        if isinstance(render_width, (int, float))
        and isinstance(render_height, (int, float))
        else "—"
    )
    render_available = bool(performance.get("render_resolution_available")) and render_resolution != "—"
    interpolation_status = str(performance.get("frame_interpolation_status") or "unavailable")
    interpolation_available = bool(performance.get("frame_interpolation_available"))
    interpolation_label = str(
        performance.get("frame_interpolation_label") or "系统未公开可验证的插帧开关"
    )
    brightness_raw = performance.get("brightness_raw")
    brightness_detail = (
        f"RenderService 背光原始值 {_number(brightness_raw, 0)}（非 nit、非热限亮）"
        if platform == "harmony"
        else f"亮度原始值 {_number(brightness_raw, 0)}"
    )
    rows = [
        ("前台窗口", performance.get("foreground_window_name") or "—", f"Window #{performance.get('foreground_window_id') or '—'}"),
        ("显示", resolution, brightness_detail),
    ]
    if render_available:
        rows.append((
            "渲染分辨率",
            render_resolution,
            str(performance.get("render_resolution_source")),
        ))
    if interpolation_available:
        rows.append((
            "插帧 / MEMC",
            interpolation_label,
            f"置信度 {performance.get('frame_interpolation_confidence') or 'low'}；不凭刷新倍率单独推断",
        ))
    if supported_text != "—":
        rows.append((
            "支持刷新率",
            supported_text,
            f"切换 {int(performance.get('refresh_switch_count') or 0)} 次",
        ))
    if performance.get("gpu_renderer"):
        rows.append((
            "GPU",
            performance.get("gpu_renderer"),
            performance.get("gpu_vendor") or "RenderService renderer",
        ))
    if hottest:
        rows.append(("最高温度", hottest_text, "仅在严重度或温升异常时形成结论"))
    if performance.get("touch_devices") or isinstance(
        performance.get("touch_interaction_count"), (int, float)
    ):
        rows.append((
            "触控交互",
            f"{int(performance.get('touch_interaction_count') or 0)} 次",
            "硬件触控扫描率未通过系统接口公开",
        ))
    return "".join(
        "<tr>"
        f"<td><strong>{_escape(label)}</strong></td>"
        f"<td>{_escape(value)}</td>"
        f'<td><span class="cell-sub">{_escape(detail)}</span></td>'
        "</tr>"
        for label, value, detail in rows
    )


def _power_pressure_driver_rows(analysis: Dict[str, object]) -> str:
    pressure = analysis.get("power_pressure", {})
    pressure = pressure if isinstance(pressure, dict) else {}
    rows = []
    for item in pressure.get("drivers", []) if isinstance(pressure.get("drivers"), list) else []:
        if not isinstance(item, dict):
            continue
        correlation = item.get("correlation")
        direction = (
            "同向"
            if isinstance(correlation, (int, float)) and float(correlation) >= 0
            else "反向"
            if isinstance(correlation, (int, float))
            else "不可用"
        )
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("label"))}</strong></td>'
            f'<td>{_number(correlation, 2)}</td>'
            f'<td>{_escape(direction)}</td>'
            f'<td>{int(item.get("sample_count") or 0)}</td>'
            f'<td><span class="cell-sub">{_escape(item.get("detail"))}</span></td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="5" class="empty-cell">当前样本不足以计算资源压力与整机功率的时间相关性。</td></tr>'


def _power_pressure_driver_visual(analysis: Dict[str, object]) -> str:
    pressure = analysis.get("power_pressure", {})
    pressure = pressure if isinstance(pressure, dict) else {}
    drivers = pressure.get("drivers", [])
    drivers = drivers if isinstance(drivers, list) else []
    rows = []
    for item in drivers[:12]:
        if not isinstance(item, dict):
            continue
        correlation = item.get("correlation")
        if not isinstance(correlation, (int, float)) or isinstance(correlation, bool):
            continue
        value = max(-1.0, min(1.0, float(correlation)))
        width = abs(value) * 50.0
        direction = "positive" if value >= 0 else "negative"
        position = f"left:50%;width:{width:.3f}%" if value >= 0 else f"right:50%;width:{width:.3f}%"
        rows.append(
            '<div class="correlation-row">'
            f'<div class="correlation-label"><strong>{_escape(item.get("label"))}</strong>'
            f'<span>{int(item.get("sample_count") or 0)} 个样本</span></div>'
            '<div class="correlation-track" aria-hidden="true"><i></i>'
            f'<span class="correlation-bar {direction}" style="{position}"></span></div>'
            f'<div class="correlation-value">{value:+.2f}</div>'
            "</div>"
        )
    if not rows:
        return ""
    return (
        '<div class="correlation-chart" role="img" aria-label="资源压力与整机功率相关系数">'
        '<div class="correlation-scale"><span>-1.0 反向</span><span>0</span><span>+1.0 同向</span></div>'
        + "".join(rows)
        + "</div>"
    )


def _power_pressure_task_rows(analysis: Dict[str, object]) -> str:
    pressure = analysis.get("power_pressure", {})
    pressure = pressure if isinstance(pressure, dict) else {}
    rows = []
    for item in pressure.get("tasks", []) if isinstance(pressure.get("tasks"), list) else []:
        if not isinstance(item, dict):
            continue
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("name"))}</strong><span class="cell-sub">{_escape(_display_label(item.get("category"), CATEGORY_LABELS))}</span></td>'
            f'<td>{_number(item.get("average_cpu_pct"))}%</td>'
            f'<td>{_number(item.get("maximum_cpu_pct"))}%</td>'
            f'<td>{_number(item.get("power_delta_when_visible_mw"), 0)} mW</td>'
            f'<td>{_number(item.get("power_correlation"), 2)}</td>'
            f'<td>{int(item.get("seen_snapshots") or 0)}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="6" class="empty-cell">没有可用的周期任务快照；仍可使用 CPU/GPU/内存频率压力分析。</td></tr>'


def _runtime_setting_rows(analysis: Dict[str, object]) -> str:
    settings = analysis.get("runtime_settings", {})
    settings = settings if isinstance(settings, dict) else {}
    rows = []
    for item in settings.get("rows", []) if isinstance(settings.get("rows"), list) else []:
        if not isinstance(item, dict):
            continue
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("label"))}</strong><span class="cell-sub">{_escape(item.get("key"))}</span></td>'
            f'<td>{_escape(item.get("start"))}</td>'
            f'<td>{_escape(item.get("end"))}</td>'
            f'<td>{"是" if item.get("changed") else "否"}</td>'
            f'<td><span class="cell-sub">{_escape(item.get("impact"))}</span></td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="5" class="empty-cell">未恢复到可读的 Android 系统设置快照。</td></tr>'


def _memory_pressure_summary(analysis: Dict[str, object]) -> str:
    memory = analysis.get("memory", {})
    memory = memory if isinstance(memory, dict) else {}
    if not memory.get("available"):
        return (
            '<div class="availability-note"><strong>内存频率不可用</strong>'
            f'<span>{_escape(memory.get("limitations") or "设备未暴露可读的 DRAM/DMC/MIF devfreq 节点。")}</span></div>'
        )
    delta = memory.get("high_frequency_power_delta_mw")
    delta_text = (
        f'高频样本比低频样本高 {_number(delta, 0)} mW'
        if isinstance(delta, (int, float))
        else "高低频样本不足以计算功率差"
    )
    return (
        '<div class="metric-grid">'
        '<article class="metric-card"><div class="metric-top"><span>平均内存频率</span><span class="source-tag counter">计数器</span></div>'
        f'<div class="metric-value">{_number(memory.get("average_frequency_mhz"), 0)} <small>MHz</small></div>'
        f'<div class="metric-context">P95 {_number(memory.get("p95_frequency_mhz"), 0)} MHz</div></article>'
        '<article class="metric-card"><div class="metric-top"><span>高频驻留</span><span class="source-tag counter">压力</span></div>'
        f'<div class="metric-value">{_number(memory.get("high_frequency_share_pct"))} <small>%</small></div>'
        f'<div class="metric-context">{_escape(delta_text)}</div></article>'
        '<article class="metric-card"><div class="metric-top"><span>功率相关性</span><span class="source-tag context">相关</span></div>'
        f'<div class="metric-value">{_number(memory.get("power_correlation"), 2)}</div>'
        '<div class="metric-context">只表示同一时间轴变化，不是内存电源轨测量</div></article>'
        '</div>'
    )


def _power_pressure_sections(analysis: Dict[str, object]) -> str:
    pressure = analysis.get("power_pressure", {})
    pressure = pressure if isinstance(pressure, dict) else {}
    memory = analysis.get("memory", {})
    memory = memory if isinstance(memory, dict) else {}
    settings = analysis.get("runtime_settings", {})
    settings = settings if isinstance(settings, dict) else {}
    sections = []
    if memory.get("available"):
        sections.append(
            '<section class="analysis-section"><h2>内存频率压力</h2>'
            + _memory_pressure_summary(analysis)
            + "</section>"
        )
    if pressure.get("drivers"):
        sections.append(
            '<section class="analysis-section"><div class="chart-toolbar"><div>'
            '<h2>资源与整机功率相关性</h2>'
            '<p class="section-copy">按同一设备时间轴展示相关方向与强度；用于筛选压力线索，不表示因果或独立电源轨功耗。</p>'
            '</div><div class="legend-row"><span><i class="legend-swatch correlation-positive"></i>同向</span>'
            '<span><i class="legend-swatch correlation-negative"></i>反向</span></div></div>'
            + _power_pressure_driver_visual(analysis)
            + '<details class="evidence-details"><summary>查看相关性计算证据</summary>'
            '<div class="data-table-wrap"><table><thead><tr><th>资源</th><th>功率相关系数</th>'
            '<th>方向</th><th>样本数</th><th>解释</th></tr></thead><tbody>'
            + _power_pressure_driver_rows(analysis)
            + "</tbody></table></div></details></section>"
        )
    if pressure.get("tasks"):
        sections.append(
            '<section class="analysis-section"><h2>任务负载压力</h2>'
            '<div class="data-table-wrap"><table><thead><tr><th>任务</th><th>平均 CPU</th>'
            '<th>峰值 CPU</th><th>可见时相对功率</th><th>功率相关性</th><th>快照数</th>'
            '</tr></thead><tbody>'
            + _power_pressure_task_rows(analysis)
            + "</tbody></table></div></section>"
        )
    if int(settings.get("changed_count") or 0) > 0:
        sections.append(
            '<section class="analysis-section"><h2>系统设置变化</h2>'
            '<div class="data-table-wrap"><table><thead><tr><th>设置</th><th>开始</th>'
            '<th>结束</th><th>变化</th><th>续航影响</th></tr></thead><tbody>'
            + _runtime_setting_rows(analysis)
            + "</tbody></table></div></section>"
        )
    return "".join(sections) or (
        '<section class="analysis-section"><div class="availability-note">'
        '<strong>没有额外功耗压力明细</strong><span>本次关闭了相关采集项，或样本不足；'
        '报告仅保留电池侧实测趋势与摘要。</span></div></section>'
    )


def _frame_flow_stages(analysis: Dict[str, object]) -> List[Dict[str, object]]:
    performance = analysis.get("performance", {})
    performance = performance if isinstance(performance, dict) else {}
    frame_flow = performance.get("frame_flow", {})
    frame_flow = frame_flow if isinstance(frame_flow, dict) else {}
    stages = frame_flow.get("stages", [])
    return [item for item in stages if isinstance(item, dict)] if isinstance(stages, list) else []


def _frame_flow_has_valid_timeline(analysis: Dict[str, object]) -> bool:
    for stage in _frame_flow_stages(analysis):
        if str(stage.get("status") or "unavailable") in {"invalid", "unavailable"}:
            continue
        timeline = stage.get("timeline", [])
        if not isinstance(timeline, list):
            continue
        for point in timeline:
            if not isinstance(point, dict):
                continue
            value = point.get(
                "value",
                point.get("frame_rate_fps", point.get("refresh_rate_hz")),
            )
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and float(value) > 0
            ):
                return True
    return False


def _frame_status_label(status: object) -> str:
    return {
        "primary": "主数据",
        "valid": "有效",
        "reference": "仅参考",
        "invalid": "无效",
        "unavailable": "无数据",
    }.get(str(status or "unavailable"), str(status or "无数据"))


def _frame_status_tag_class(status: object) -> str:
    return {
        "primary": "measured",
        "valid": "counter",
        "reference": "model",
        "invalid": "high",
        "unavailable": "low",
    }.get(str(status or "unavailable"), "low")


def _frame_stage_value(item: Dict[str, object]) -> str:
    unit = str(item.get("unit") or "")
    digits = 0 if unit == "Hz" else 2 if unit == "ms" else 1
    if not isinstance(item.get("value"), (int, float)) or isinstance(item.get("value"), bool):
        return "—"
    return f'{_number(item.get("value"), digits)} {_escape(unit)}'.strip()


def _frame_flow_visual(analysis: Dict[str, object]) -> str:
    stages = _frame_flow_stages(analysis)
    if not stages:
        return (
            '<div class="availability-note"><strong>没有可判定的帧率数据流</strong>'
            '<span>本次未恢复到与目标应用绑定且持续产生有效增量的帧计数来源。</span></div>'
        )
    cards = []
    for index, item in enumerate(stages):
        status = str(item.get("status") or "unavailable")
        metrics = []
        metric_items = item.get("metrics", [])
        metric_items = metric_items if isinstance(metric_items, list) else []
        for metric in metric_items[:3]:
            if not isinstance(metric, dict):
                continue
            value = metric.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            digits = int(metric.get("digits") or 0)
            metrics.append(
                '<span><small>'
                + _escape(metric.get("label"))
                + '</small><strong>'
                + _number(value, digits)
                + (' ' + _escape(metric.get("unit")) if metric.get("unit") else '')
                + '</strong></span>'
            )
        cards.append(
            f'<article class="frame-stage" data-status="{_escape(status)}">'
            '<div class="frame-stage-top">'
            f'<span class="frame-stage-index">{index + 1:02d}</span>'
            f'<span class="source-tag {_frame_status_tag_class(status)}">{_escape(_frame_status_label(status))}</span>'
            '</div>'
            f'<div class="frame-stage-phase">{_escape(item.get("phase") or "STAGE")}</div>'
            f'<h3>{_escape(item.get("label") or item.get("key"))}</h3>'
            f'<div class="frame-stage-value">{_frame_stage_value(item)}</div>'
            f'<div class="frame-stage-caption">{_escape(item.get("value_label") or "当前阶段")}</div>'
            + (f'<div class="frame-stage-metrics">{"".join(metrics)}</div>' if metrics else '')
            + f'<div class="frame-stage-source">{_escape(item.get("source") or "来源未记录")}</div>'
            + "</article>"
        )
    return '<div class="frame-flow-visual">' + '<span class="frame-flow-arrow" aria-hidden="true">→</span>'.join(cards) + "</div>"


def _frame_flow_history_section(analysis: Dict[str, object]) -> str:
    if not _frame_flow_has_valid_timeline(analysis):
        return ""
    return (
        '<section class="analysis-section frame-flow-history-report">'
        '<div class="chart-toolbar"><div><h2>完整链路节点帧率趋势</h2>'
        '<p class="section-copy">在同一时间轴上分别展示应用提交、合成器呈现与显示刷新率。'
        '平台未公开独立帧计数的节点保留为空轨，不使用阶段延迟冒充 FPS。</p></div></div>'
        + _frame_flow_visual(analysis)
        + '<div class="chart-surface frame-flow-history-surface">'
        '<svg id="frame-flow-history-chart" role="img" aria-label="完整渲染链路节点帧率与刷新率趋势"></svg>'
        '</div>'
        + _frame_flow_evidence(analysis)
        + '</section>'
    )


def _frame_flow_evidence(analysis: Dict[str, object]) -> str:
    if not _frame_flow_stages(analysis):
        return ""
    return (
        '<details class="evidence-details"><summary>查看逐阶段数据源与有效性证据</summary>'
        '<div class="data-table-wrap"><table><thead><tr><th>渲染阶段</th><th>状态</th>'
        '<th>速率 / 耗时</th><th>数据来源</th><th>样本数</th><th>判定说明</th></tr></thead>'
        '<tbody>' + _frame_flow_rows(analysis) + "</tbody></table></div></details>"
    )


def _frame_interval_values(analysis: Dict[str, object]) -> List[float]:
    performance = analysis.get("performance", {})
    performance = performance if isinstance(performance, dict) else {}
    timeline = performance.get("frame_rate_timeline", [])
    timeline = timeline if isinstance(timeline, list) else []
    values = []
    for item in timeline:
        if not isinstance(item, dict):
            continue
        intervals = item.get("frame_intervals_ms", [])
        intervals = intervals if isinstance(intervals, list) else []
        for value in intervals:
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and 0 < float(value) < 10_000
            ):
                values.append(float(value))
    return values


def _percentile_value(values: List[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = max(0.0, min(1.0, quantile)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _frame_budget_ms(analysis: Dict[str, object]) -> float | None:
    performance = analysis.get("performance", {})
    performance = performance if isinstance(performance, dict) else {}
    timeline = performance.get("frame_rate_timeline", [])
    timeline = timeline if isinstance(timeline, list) else []
    refresh_values = {
        float(item.get("refresh_rate_hz"))
        for item in timeline
        if isinstance(item, dict)
        and isinstance(item.get("frame_intervals_ms"), list)
        and bool(item.get("frame_intervals_ms"))
        and isinstance(item.get("refresh_rate_hz"), (int, float))
        and not isinstance(item.get("refresh_rate_hz"), bool)
        and float(item.get("refresh_rate_hz")) > 0
    }
    if len(refresh_values) != 1:
        return None
    return 1000.0 / next(iter(refresh_values))


def _frame_interval_budget_pairs(
    analysis: Dict[str, object],
) -> List[tuple[float, float, float]]:
    performance = analysis.get("performance", {})
    performance = performance if isinstance(performance, dict) else {}
    timeline = performance.get("frame_rate_timeline", [])
    timeline = timeline if isinstance(timeline, list) else []
    pairs: List[tuple[float, float, float]] = []
    for item in timeline:
        if not isinstance(item, dict):
            continue
        refresh = item.get("refresh_rate_hz")
        if (
            not isinstance(refresh, (int, float))
            or isinstance(refresh, bool)
            or float(refresh) <= 0
        ):
            continue
        budget = 1000.0 / float(refresh)
        intervals = item.get("frame_intervals_ms", [])
        intervals = intervals if isinstance(intervals, list) else []
        for value in intervals:
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and 0 < float(value) < 10_000
            ):
                pairs.append((float(value), budget, float(refresh)))
    return pairs


def _frame_stability_section(analysis: Dict[str, object]) -> str:
    values = _frame_interval_values(analysis)
    if not values:
        return ""
    budget_pairs = _frame_interval_budget_pairs(analysis)
    budget = _frame_budget_ms(analysis)
    within_count = sum(1 for value, item_budget, _ in budget_pairs if value <= item_budget)
    within_pct = (
        within_count / len(budget_pairs) * 100.0 if budget_pairs else None
    )
    issue_count = sum(
        1 for value, item_budget, _ in budget_pairs if value > item_budget * 1.5
    )
    issue_pct = issue_count / len(budget_pairs) * 100.0 if budget_pairs else None
    issue_threshold = budget * 1.5 if budget is not None else None
    p99 = _percentile_value(values, 0.99)
    maximum = max(values)
    refresh_budgets = sorted(
        {(refresh, item_budget) for _, item_budget, refresh in budget_pairs},
        key=lambda item: item[0],
    )
    if len(refresh_budgets) == 1:
        budget_label = f"{refresh_budgets[0][1]:.2f} ms（{refresh_budgets[0][0]:.0f} Hz）"
    elif refresh_budgets:
        budget_label = "按窗口动态（" + " / ".join(
            f"{refresh:.0f} Hz {item_budget:.2f} ms"
            for refresh, item_budget in refresh_budgets
        ) + "）"
    else:
        budget_label = "未能与逐窗口刷新率对齐"
    budget_attr = f'{budget:.8f}' if budget is not None else ""
    issue_attr = f'{issue_threshold:.8f}' if issue_threshold is not None else ""
    budget_lines_attr = _escape(
        json.dumps(
            [
                {"refresh_hz": refresh, "budget_ms": item_budget}
                for refresh, item_budget in refresh_budgets
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )
    within_label = f"{within_pct:.2f}%" if within_pct is not None else "—"
    issue_label = f"{issue_pct:.2f}%" if issue_pct is not None else "—"
    aligned_label = (
        f"{len(budget_pairs):,} / {len(values):,} 帧已对齐刷新率"
        if budget_pairs
        else "没有逐帧刷新率对齐证据"
    )
    return (
        '<section class="analysis-section frame-stability-section">'
        '<div class="chart-toolbar"><div><h2>帧间隔分布</h2>'
        '<p class="section-copy">每个帧窗口按该窗口自己的刷新率换算预算；动态刷新会分别使用 60/90/120 Hz 等对应预算，无法逐窗口对齐的帧只进入分布，不参与跨预算比例结论。</p>'
        '</div><div class="legend-row"><span><i class="legend-swatch histogram-normal"></i>单周期内</span>'
        '<span><i class="legend-swatch histogram-edge"></i>预算边缘</span>'
        '<span><i class="legend-swatch histogram-tail"></i>长帧（&gt;1.5×预算）</span></div></div>'
        '<div class="stability-metrics">'
        f'<div><span>帧间隔样本</span><strong>{len(values):,}</strong></div>'
        f'<div><span>帧预算</span><strong>{_escape(budget_label)}</strong></div>'
        f'<div><span>单周期内 / 长帧（&gt;1.5×预算）</span><strong>{_escape(within_label)} / {_escape(issue_label)}</strong></div>'
        f'<div><span>P99 / 最大</span><strong>{_number(p99, 2)} / {_number(maximum, 2)} ms</strong></div>'
        '</div>'
        f'<p class="section-copy">{_escape(aligned_label)}。长帧比例由逐帧间隔按 1.5×刷新预算重新计算；它与 SurfaceFlinger / 平台计数器报告的截止时间未命中或异常帧不是同一口径，数值不要求相等。</p>'
        '<div class="chart-surface frame-interval-surface">'
        f'<svg id="frame-interval-chart" data-budget-ms="{_escape(budget_attr)}" data-issue-ms="{_escape(issue_attr)}" data-budget-lines="{budget_lines_attr}" role="img" aria-label="帧间隔直方图"></svg>'
        '</div></section>'
    )


def _render_pipeline_rows(analysis: Dict[str, object]) -> str:
    render = analysis.get("render_performance", {})
    render = render if isinstance(render, dict) else {}
    pipeline = render.get("pipeline", {})
    pipeline = pipeline if isinstance(pipeline, dict) else {}
    rows = []
    for item in pipeline.get("stages", []) if isinstance(pipeline.get("stages"), list) else []:
        if not isinstance(item, dict):
            continue
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("label"))}</strong></td>'
            f'<td>{int(item.get("sample_count") or 0)}</td>'
            f'<td>{_number(item.get("average_ms"), 2)} ms</td>'
            f'<td>{_number(item.get("p95_ms"), 2)} ms</td>'
            f'<td>{_number(item.get("p99_ms"), 2)} ms</td>'
            f'<td>{_number(item.get("maximum_ms"), 2)} ms</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="6" class="empty-cell">当前平台没有恢复到可用的详细 framestats 阶段时间戳。</td></tr>'


def _frame_flow_rows(analysis: Dict[str, object]) -> str:
    performance = analysis.get("performance", {})
    performance = performance if isinstance(performance, dict) else {}
    frame_flow = performance.get("frame_flow", {})
    frame_flow = frame_flow if isinstance(frame_flow, dict) else {}
    stages = frame_flow.get("stages", [])
    stages = stages if isinstance(stages, list) else []
    status_labels = {
        "primary": "主数据",
        "valid": "有效",
        "reference": "仅参考",
        "invalid": "无效",
        "unavailable": "无数据",
    }
    status_classes = {
        "primary": "measured",
        "valid": "counter",
        "reference": "medium",
        "invalid": "high",
        "unavailable": "low",
    }
    rows = []
    for index, item in enumerate(stages, start=1):
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "unavailable")
        unit = str(item.get("unit") or "")
        digits = 0 if unit == "Hz" else 2 if unit == "ms" else 1
        value = (
            f'{_number(item.get("value"), digits)} {_escape(unit)}'
            if isinstance(item.get("value"), (int, float))
            else "—"
        )
        rows.append(
            "<tr>"
            f'<td><strong>{index:02d} · {_escape(item.get("phase") or "STAGE")}</strong>'
            f'<span class="cell-sub">{_escape(item.get("label") or item.get("key"))}</span></td>'
            f'<td><span class="source-tag {status_classes.get(status, "low")}">'
            f'{_escape(status_labels.get(status, status))}</span></td>'
            f'<td><strong>{value}</strong><span class="cell-sub">{_escape(item.get("value_label"))}</span></td>'
            f'<td>{_escape(item.get("source") or "未记录来源")}</td>'
            f'<td>{int(item.get("sample_count") or 0) if isinstance(item.get("sample_count"), (int, float)) else "—"}</td>'
            f'<td>{_escape(item.get("detail") or "暂无判定说明")}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="6" class="empty-cell">本次没有形成可判定的帧率数据流。</td></tr>'


def _slow_frame_rows(analysis: Dict[str, object]) -> str:
    render = analysis.get("render_performance", {})
    render = render if isinstance(render, dict) else {}
    pipeline = render.get("pipeline", {})
    pipeline = pipeline if isinstance(pipeline, dict) else {}
    rows = []
    for item in pipeline.get("slow_frames", [])[:20] if isinstance(pipeline.get("slow_frames"), list) else []:
        if not isinstance(item, dict):
            continue
        stage_values = [
            (key, value)
            for key, value in item.items()
            if str(key).endswith("_ms")
            and key != "total_ms"
            and isinstance(value, (int, float))
        ]
        dominant_key, dominant_value = max(stage_values, key=lambda pair: float(pair[1])) if stage_values else ("—", None)
        rows.append(
            "<tr>"
            f'<td>{_escape(item.get("frame_id"))}</td>'
            f'<td><strong>{_number(item.get("total_ms"), 2)} ms</strong><span class="cell-sub">{_escape(item.get("window"))}</span></td>'
            f'<td>{"是" if item.get("deadline_missed") else "否"}</td>'
            f'<td>{_escape(dominant_key.replace("_ms", ""))}</td>'
            f'<td>{_number(dominant_value, 2)} ms</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="5" class="empty-cell">没有可展示的详细慢帧记录。</td></tr>'


def _render_thread_rows(analysis: Dict[str, object]) -> str:
    render = analysis.get("render_performance", {})
    render = render if isinstance(render, dict) else {}
    rows = []
    for item in render.get("render_threads", []) if isinstance(render.get("render_threads"), list) else []:
        if not isinstance(item, dict):
            continue
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("name"))}</strong><span class="cell-sub">{_escape(item.get("process"))}</span></td>'
            f'<td>{_escape(item.get("pid"))} / {_escape(item.get("tid"))}</td>'
            f'<td>{_number(item.get("average_when_visible_cpu_pct"))}%</td>'
            f'<td>{_number(item.get("maximum_cpu_pct"))}%</td>'
            f'<td>{int(item.get("seen_snapshots") or 0)}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="5" class="empty-cell">未恢复到 RenderThread、SurfaceFlinger、RenderEngine 或 Composer 热点线程。</td></tr>'


def _performance_process_rows(analysis: Dict[str, object]) -> str:
    system = analysis.get("system", {})
    system = system if isinstance(system, dict) else {}
    rows = []
    for item in system.get("top_processes", [])[:10] if isinstance(system.get("top_processes"), list) else []:
        if not isinstance(item, dict):
            continue
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("name") or item.get("command"))}</strong><span class="cell-sub">{_escape(item.get("user"))}</span></td>'
            f'<td>{_number(item.get("average_cpu_pct"))}%</td>'
            f'<td>{_number(item.get("average_when_visible_cpu_pct"))}%</td>'
            f'<td>{_number(item.get("maximum_cpu_pct"))}%</td>'
            f'<td>{int(item.get("seen_snapshots") or 0)}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="5" class="empty-cell">没有可用的进程调度快照。</td></tr>'


def _render_pipeline_data(analysis: Dict[str, object]) -> Dict[str, object]:
    render = analysis.get("render_performance", {})
    render = render if isinstance(render, dict) else {}
    pipeline = render.get("pipeline", {})
    return pipeline if isinstance(pipeline, dict) else {}


def _render_pipeline_section(analysis: Dict[str, object]) -> str:
    pipeline = _render_pipeline_data(analysis)
    stages = pipeline.get("stages", [])
    stages = [item for item in stages if isinstance(item, dict)] if isinstance(stages, list) else []
    if not stages:
        return ""
    return (
        '<section class="analysis-section"><h2>阶段耗时分布</h2>'
        '<details class="evidence-details"><summary>查看详细 framestats 阶段统计</summary>'
        '<div class="data-table-wrap"><table><thead><tr><th>阶段</th><th>帧数</th><th>平均</th>'
        '<th>P95</th><th>P99</th><th>峰值</th></tr></thead><tbody>'
        + _render_pipeline_rows(analysis)
        + "</tbody></table></div></details></section>"
    )


def _slow_frame_section(analysis: Dict[str, object]) -> str:
    pipeline = _render_pipeline_data(analysis)
    slow_frames = pipeline.get("slow_frames", [])
    slow_frames = [item for item in slow_frames if isinstance(item, dict)] if isinstance(slow_frames, list) else []
    if not slow_frames:
        return ""
    return (
        '<section class="analysis-section"><h2>慢帧明细</h2>'
        '<details class="evidence-details"><summary>查看最慢 20 帧的阶段证据</summary>'
        '<div class="data-table-wrap"><table><thead><tr><th>帧 ID</th><th>总耗时</th>'
        '<th>超截止时间</th><th>最大阶段</th><th>阶段耗时</th></tr></thead><tbody>'
        + _slow_frame_rows(analysis)
        + "</tbody></table></div></details></section>"
    )


def _render_thread_section(analysis: Dict[str, object], heading: str = "渲染与合成热点线程") -> str:
    render = analysis.get("render_performance", {})
    render = render if isinstance(render, dict) else {}
    threads = render.get("render_threads", [])
    threads = [item for item in threads if isinstance(item, dict)] if isinstance(threads, list) else []
    if not threads:
        return ""
    return (
        f'<section class="analysis-section"><h2>{_escape(heading)}</h2>'
        '<details class="evidence-details"><summary>查看线程 CPU 快照</summary>'
        '<div class="data-table-wrap"><table><thead><tr><th>线程</th><th>PID / TID</th>'
        '<th>可见时平均 CPU</th><th>峰值 CPU</th><th>快照数</th></tr></thead><tbody>'
        + _render_thread_rows(analysis)
        + "</tbody></table></div></details></section>"
    )


def _performance_process_section(analysis: Dict[str, object]) -> str:
    system = analysis.get("system", {})
    system = system if isinstance(system, dict) else {}
    processes = system.get("top_processes", [])
    processes = [item for item in processes if isinstance(item, dict)] if isinstance(processes, list) else []
    if not processes:
        return ""
    return (
        '<section class="analysis-section"><h2>进程调度热点</h2>'
        '<details class="evidence-details"><summary>查看周期进程 CPU 快照</summary>'
        '<div class="data-table-wrap"><table><thead><tr><th>进程</th><th>全程平均 CPU</th>'
        '<th>进入 Top 时平均</th><th>峰值</th><th>快照数</th></tr></thead><tbody>'
        + _performance_process_rows(analysis)
        + "</tbody></table></div></details></section>"
    )


def _refresh_residency_available(analysis: Dict[str, object]) -> bool:
    performance = analysis.get("performance", {})
    performance = performance if isinstance(performance, dict) else {}
    rows = performance.get("refresh_residency", [])
    rows = rows if isinstance(rows, list) else []
    return any(
        isinstance(item, dict)
        and isinstance(item.get("refresh_rate_hz"), (int, float))
        and not isinstance(item.get("refresh_rate_hz"), bool)
        for item in rows
    )


def _performance_context_available(analysis: Dict[str, object]) -> bool:
    performance = analysis.get("performance", {})
    performance = performance if isinstance(performance, dict) else {}
    thermal = analysis.get("thermal", {})
    thermal = thermal if isinstance(thermal, dict) else {}
    return any(
        (
            performance.get("foreground_window_name"),
            isinstance(performance.get("foreground_window_id"), (int, float)),
            isinstance(performance.get("display_width_px"), (int, float)),
            isinstance(performance.get("display_height_px"), (int, float)),
            bool(performance.get("render_resolution_available")),
            bool(performance.get("frame_interpolation_available")),
            bool(performance.get("supported_refresh_rates_hz")),
            performance.get("gpu_renderer"),
            bool(thermal.get("hottest_sensor")),
            bool(performance.get("touch_devices")),
            isinstance(performance.get("touch_interaction_count"), (int, float)),
        )
    )


def _performance_context_section(
    analysis: Dict[str, object],
    heading: str = "关键上下文",
) -> str:
    if not _performance_context_available(analysis):
        return ""
    return (
        f'<section class="analysis-section"><h2>{_escape(heading)}</h2>'
        '<div class="data-table-wrap"><table><thead><tr><th>项目</th><th>当前值</th>'
        '<th>说明</th></tr></thead><tbody>' + _performance_context_rows(analysis) + "</tbody></table></div></section>"
    )


def _performance_context_sections(analysis: Dict[str, object]) -> str:
    sections = []
    if _refresh_residency_available(analysis):
        sections.append(
            '<section class="analysis-section"><h2>刷新档位驻留</h2>'
            '<div class="residency-list">' + _refresh_residency_rows(analysis) + "</div></section>"
        )
    context = _performance_context_section(analysis)
    if context:
        sections.append(context)
    if not sections:
        return ""
    return '<div class="split-layout">' + "".join(sections) + "</div>" if len(sections) > 1 else sections[0]


def _cpu_process_section(analysis: Dict[str, object]) -> str:
    processes = analysis.get("processes", [])
    processes = [item for item in processes if isinstance(item, dict)] if isinstance(processes, list) else []
    if not processes:
        return ""
    return (
        '<section class="analysis-section"><h2>进程 CPU 快照</h2>'
        '<div class="data-table-wrap"><table><thead><tr><th>进程</th><th>总占用</th>'
        '<th>用户态</th><th>内核态</th></tr></thead><tbody>'
        + _process_rows(analysis)
        + "</tbody></table></div></section>"
    )


def _gpu_live_available(analysis: Dict[str, object]) -> bool:
    gpu = analysis.get("gpu", {})
    gpu = gpu if isinstance(gpu, dict) else {}
    return bool(gpu.get("frequency_available") or gpu.get("load_available"))


def _gpu_power_fallback_available(analysis: Dict[str, object]) -> bool:
    gpu = analysis.get("gpu", {})
    gpu = gpu if isinstance(gpu, dict) else {}
    memory = gpu.get("memory", {})
    memory = memory if isinstance(memory, dict) else {}
    work = gpu.get("work_by_uid", [])
    return bool((isinstance(work, list) and work) or memory.get("available"))


def _gpu_report_available(analysis: Dict[str, object], test_mode: str) -> bool:
    return _gpu_live_available(analysis) or (
        test_mode == "power" and _gpu_power_fallback_available(analysis)
    )


def _gpu_unavailable_display_reason(gpu: Dict[str, object]) -> str:
    reason = str(gpu.get("unavailable_reason") or "").strip()
    if reason.startswith("No readable GPU frequency/load node"):
        return "ADB shell 未暴露可读的 GPU 频率或负载节点，Perfetto 也未注册 GPU 硬件计数器数据源。"
    if reason.startswith("DVT Graphics"):
        return "iOS DVT Graphics 没有返回可用的 GPU 利用率事件。"
    return reason or "设备未暴露可读的 GPU 频率或负载计数器。"


def _report_warning_items(analysis: Dict[str, object], test_mode: str) -> List[str]:
    warnings = analysis.get("warnings", [])
    warnings = warnings if isinstance(warnings, list) else []
    gpu_available = _gpu_report_available(analysis, test_mode)
    findings = analysis.get("findings", [])
    findings = findings if isinstance(findings, list) else []
    has_observer_finding = any(
        isinstance(item, dict)
        and str(item.get("title") or "") == "观察者相关进程 CPU 上界"
        for item in findings
    )
    has_observer_average_warning = any(
        str(item or "").startswith("Average normalized iOS collector CPU overhead was ")
        for item in warnings
    )
    rows = []
    for item in warnings:
        warning = str(item or "").strip()
        if not warning:
            continue
        if warning.startswith(
            "iOS DVT sysmond/DTServiceHub/remotepairingdeviced add measurable collection overhead;"
        ) and (has_observer_average_warning or has_observer_finding):
            continue
        if has_observer_finding and warning.startswith(
            "Average normalized iOS collector CPU overhead was "
        ):
            continue
        if not gpu_available and "gpu" in warning.lower() and (
            "回退证据" in warning
            or "报告将使用可用的 gpu 负载" in warning.lower()
            or "generic_sysfs" in warning.lower()
        ):
            continue
        rows.append(localize_collection_warning(warning))
    return rows


def _analysis_coverage_section(
    analysis: Dict[str, object],
    test_mode: str,
    gpu_report_available: bool,
) -> str:
    rows: List[Tuple[str, str, str, str]] = []
    gpu = analysis.get("gpu", {})
    gpu = gpu if isinstance(gpu, dict) else {}
    performance = analysis.get("performance", {})
    performance = performance if isinstance(performance, dict) else {}
    capture_configuration = analysis.get("capture_configuration", {})
    capture_configuration = (
        capture_configuration if isinstance(capture_configuration, dict) else {}
    )
    features = capture_configuration.get("features", {})
    features = features if isinstance(features, dict) else {}
    backend = str(capture_configuration.get("backend") or "")
    platform = str(
        analysis.get("platform")
        or ("harmony" if backend.startswith("harmony") else "android")
    ).lower()
    memory = analysis.get("memory", {})
    memory = memory if isinstance(memory, dict) else {}
    frame_unavailable_reason = str(performance.get("frame_unavailable_reason") or "")
    if "display is inactive" in frame_unavailable_reason.lower():
        frame_unavailable_reason = "屏幕未处于活动状态；保留的刷新配置不代表当前帧输出。"

    def feature_enabled(name: str) -> bool:
        return bool(features.get(name)) if features else True

    def memory_frequency_unavailable_reason() -> str:
        if backend == "harmony_smartperf":
            return "本次 SmartPerf SP_daemon -d 未返回可用的 DDR 频率字段，因此不能生成内存频率时间线或压力结论。"
        return str(
            memory.get("reason")
            or memory.get("limitations")
            or "设备未暴露可读内存频率节点，或该采集项已关闭。"
        )

    if test_mode == "performance":
        frame_stages = _frame_flow_stages(analysis)
        invalid_stages = [
            item for item in frame_stages
            if str(item.get("status") or "unavailable") in {"invalid", "unavailable"}
        ]
        if invalid_stages and feature_enabled("frame_rate"):
            labels = "、".join(str(item.get("phase") or item.get("label") or "阶段") for item in invalid_stages)
            usable_stage_count = sum(
                1
                for item in frame_stages
                if str(item.get("status") or "unavailable")
                in {"primary", "valid", "reference"}
            )
            rows.append((
                "帧数据源有效性",
                "部分可用" if usable_stage_count else "未覆盖",
                "无效来源不参与主 FPS" if usable_stage_count else "空图表已省略",
                f"{labels} 未形成有效增量；保留在渲染链路证据明细中说明原因。",
            ))
        pipeline = _render_pipeline_data(analysis)
        stages = pipeline.get("stages", [])
        if feature_enabled("frame_details") and (not isinstance(stages, list) or not stages):
            if platform == "harmony":
                jitter_available = isinstance(
                    performance.get("frame_metric_p95_ms"), (int, float)
                )
                rows.append((
                    "详细渲染阶段 / 慢帧",
                    "仅帧抖动" if jitter_available else "未覆盖",
                    "fpsJitters" if jitter_available else "空表已省略",
                    (
                        "SmartPerf fpsJitters 可用于 P95/P99 与慢帧统计，但量产接口不提供 "
                        "RenderThread、BufferQueue、GPU 或 HWC 阶段时间戳。"
                        if jitter_available
                        else "本次 SmartPerf 未获得可用 fpsJitters；量产接口即使返回帧抖动，"
                        "也不能拆分 RenderThread、BufferQueue、GPU 或 HWC 阶段。"
                    ),
                ))
            else:
                rows.append((
                    "详细渲染阶段 / 慢帧",
                    "未覆盖",
                    "空表已省略",
                    "目标窗口没有产生可解析的 framestats 阶段时间戳，不能定位引擎内部、RenderThread 或 HWC 分段时延。",
                ))
        render = analysis.get("render_performance", {})
        render = render if isinstance(render, dict) else {}
        if (
            feature_enabled("hot_threads") or feature_enabled("process_snapshots")
        ) and not render.get("render_threads"):
            rows.append((
                "渲染 / 合成线程热点",
                "未覆盖",
                "空表已省略",
                "未采集到 RenderThread、SurfaceFlinger、RenderEngine 或 Composer 的周期线程快照。",
            ))
        system = analysis.get("system", {})
        system = system if isinstance(system, dict) else {}
        if feature_enabled("process_snapshots") and not system.get("top_processes"):
            rows.append((
                "进程调度热点",
                "未覆盖",
                "空表已省略",
                "本次没有周期进程快照；不能据此判断后台进程调度竞争。",
            ))
        if feature_enabled("frame_rate") and not _frame_interval_values(analysis):
            rows.append((
                "帧间隔分布",
                "未覆盖",
                "图表已省略",
                frame_unavailable_reason or "帧数据源没有返回逐帧间隔样本。",
            ))
        if (
            feature_enabled("foreground_window") or feature_enabled("frame_rate")
        ) and not _refresh_residency_available(analysis):
            refresh_reason = str(
                performance.get("refresh_rate_unavailable_reason")
                or frame_unavailable_reason
                or "平台没有提供会话内可计算的刷新档位计数变化。"
            )
            rows.append((
                "刷新档位驻留",
                "未覆盖",
                "空模块已省略",
                refresh_reason,
            ))
        if (
            feature_enabled("foreground_window")
            and performance.get("render_resolution_available") is False
        ):
            rows.append((
                "游戏内部渲染分辨率",
                "未覆盖",
                "不展示推测值",
                "当前只能验证显示尺寸或前台 Surface 缓冲区；公开接口没有提供可确认的游戏引擎内部渲染分辨率，因此不会把显示分辨率或估算缩放值冒充实测。",
            ))
        if feature_enabled("gpu_metrics") and not gpu_report_available:
            rows.append((
                "GPU 实时遥测",
                "未覆盖",
                "GPU 页面已省略",
                _gpu_unavailable_display_reason(gpu),
            ))
        if feature_enabled("memory_frequency") and not memory.get("available"):
            rows.append((
                "内存频率",
                "未覆盖",
                "时间线与分析模块已省略",
                memory_frequency_unavailable_reason(),
            ))
    else:
        pressure = analysis.get("power_pressure", {})
        pressure = pressure if isinstance(pressure, dict) else {}
        settings = analysis.get("runtime_settings", {})
        settings = settings if isinstance(settings, dict) else {}
        if any(
            feature_enabled(name)
            for name in ("cpu_usage", "cpu_frequency", "gpu_metrics", "thermal")
        ) and not pressure.get("drivers"):
            rows.append(("资源功率相关性", "未覆盖", "图表已省略", "相关采集项已关闭，或有效时间样本不足。"))
        if any(
            feature_enabled(name) for name in ("target_process", "process_snapshots")
        ) and not pressure.get("tasks"):
            rows.append(("任务负载压力", "未覆盖", "空表已省略", "没有可用的周期任务快照。"))
        if feature_enabled("memory_frequency") and not memory.get("available"):
            rows.append((
                "内存频率压力",
                "未覆盖",
                "空模块已省略",
                memory_frequency_unavailable_reason(),
            ))
        if feature_enabled("runtime_settings") and not settings.get("rows"):
            rows.append(("系统设置变化", "未覆盖", "空表已省略", "未恢复到可比较的测试前后系统设置快照。"))
        if feature_enabled("gpu_metrics") and not gpu_report_available:
            rows.append((
                "GPU 证据",
                "未覆盖",
                "GPU 页面已省略",
                _gpu_unavailable_display_reason(gpu),
            ))
    if feature_enabled("process_snapshots") and not analysis.get("processes"):
        rows.append(("进程 CPU 快照", "未覆盖", "空表已省略", "采样中没有可用的进程 CPU 明细。"))
    if not rows:
        return ""
    body = "".join(
        "<tr>"
        f"<td><strong>{_escape(label)}</strong></td>"
        f'<td><span class="source-tag {"medium" if status == "部分可用" else "low"}">{_escape(status)}</span></td>'
        f"<td>{_escape(action)}</td>"
        f'<td><span class="cell-sub">{_escape(reason)}</span></td>'
        "</tr>"
        for label, status, action, reason in rows
    )
    return (
        '<section class="analysis-section"><h2>分析覆盖与省略项</h2>'
        '<p class="section-copy">没有有效证据的分析不会占用主报告页面；以下项目仅说明采集边界，不形成性能或功耗结论。</p>'
        '<div class="data-table-wrap"><table><thead><tr><th>分析项</th><th>覆盖状态</th>'
        '<th>报告处理</th><th>原因</th></tr></thead><tbody>' + body + "</tbody></table></div></section>"
    )


def _performance_interference_status(analysis: Dict[str, object]) -> str:
    render = analysis.get("render_performance", {})
    render = render if isinstance(render, dict) else {}
    bottlenecks = render.get("bottlenecks", [])
    bottlenecks = bottlenecks if isinstance(bottlenecks, list) else []
    if not bottlenecks:
        performance = analysis.get("performance", {})
        performance = performance if isinstance(performance, dict) else {}
        frame_count = performance.get("frame_sample_count")
        frame_issue_pct = performance.get("frame_issue_pct")
        if not isinstance(frame_issue_pct, (int, float)):
            frame_issue_pct = performance.get("missed_vsync_interval_pct")
        has_frame_evidence = (
            isinstance(frame_count, (int, float))
            and not isinstance(frame_count, bool)
            and float(frame_count) > 0
        )
        has_frame_issue = (
            isinstance(frame_issue_pct, (int, float))
            and not isinstance(frame_issue_pct, bool)
            and float(frame_issue_pct) >= 2.0
        ) or int(performance.get("severe_frame_interval_count") or 0) > 0
        pipeline = _render_pipeline_data(analysis)
        pipeline_available = bool(pipeline.get("available") or pipeline.get("stages"))
        if has_frame_issue:
            frame_issue_label = str(
                performance.get("frame_issue_label") or "超出帧预算或截止时间"
            )
            return (
                '<div class="priority-callout active"><span class="status-dot warning"></span><div>'
                '<strong>检测到帧节奏异常，但尚未定位到具体渲染阶段</strong>'
                f'<span>{_number(frame_issue_pct, 2)}% 帧{_escape(frame_issue_label)}。'
                + (
                    "详细阶段时间戳可用，但尚未形成稳定的主导瓶颈。"
                    if pipeline_available
                    else "详细 framestats 未覆盖当前渲染面，不能据此推断引擎、RenderThread 或 HWC 内部耗时。"
                )
                + "</span></div></div>"
            )
        if has_frame_evidence:
            return (
                '<div class="priority-callout"><span class="status-dot good"></span><div>'
                '<strong>当前呈现帧节奏未触发异常阈值</strong>'
                f'<span>已覆盖 {int(frame_count)} 个帧间隔样本'
                + (f"，异常占比 {_number(frame_issue_pct, 2)}%" if isinstance(frame_issue_pct, (int, float)) else "")
                + (
                    "；详细阶段证据可继续用于定位内部时延。"
                    if pipeline_available
                    else "；详细渲染阶段未公开，因此该结论仅描述最终呈现节奏，不代表内部链路没有等待。"
                )
                + "</span></div></div>"
            )
        return (
            '<div class="priority-callout active"><span class="status-dot warning"></span><div>'
            '<strong>帧延迟证据不足，未进行瓶颈判定</strong>'
            '<span>缺少有效帧间隔或截止时间样本；报告不会把“无法采集”解释为“没有性能问题”。</span>'
            '</div></div>'
        )
    leading = bottlenecks[0] if isinstance(bottlenecks[0], dict) else {}
    return (
        '<div class="priority-callout active"><span class="status-dot warning"></span><div>'
        f'<strong>主要帧延迟线索：{_escape(leading.get("stage") or "渲染链路")}</strong>'
        f'<span>{_escape(leading.get("detail"))}</span>'
        '</div></div>'
    )


def _performance_power_recording(analysis: Dict[str, object]) -> str:
    render = analysis.get("render_performance", {})
    render = render if isinstance(render, dict) else {}
    power = render.get("power_recording", {})
    power = power if isinstance(power, dict) else {}
    return (
        '<div class="availability-note"><strong>整机功耗仅记录</strong>'
        f'<span>平均 {_number(power.get("average_power_mw"), 0)} mW · '
        f'P95 {_number(power.get("p95_power_mw"), 0)} mW · '
        f'峰值 {_number(power.get("maximum_power_mw"), 0)} mW · '
        f'能量 {_number(power.get("energy_mwh"), 2)} mWh。'
        '性能模式不继续拆分组件、UID、Wakelock 或第三方任务功耗来源。</span></div>'
    )


def _performance_memory_frequency_available(analysis: Dict[str, object]) -> bool:
    memory = analysis.get("memory", {})
    memory = memory if isinstance(memory, dict) else {}
    if bool(memory.get("analysis_disabled")):
        return False
    if bool(memory.get("available")) and not bool(memory.get("analysis_disabled")):
        return True
    test_items = analysis.get("test_items", {})
    test_items = test_items if isinstance(test_items, dict) else {}
    rows = test_items.get("rows", [])
    return any(
        isinstance(item, dict)
        and (
            isinstance(item.get("average_memory_frequency_mhz"), (int, float))
            or isinstance(item.get("p95_memory_frequency_mhz"), (int, float))
        )
        for item in rows if isinstance(rows, list)
    )


def _performance_test_item_is_substantive(item: Dict[str, object]) -> bool:
    frame_evidence = any(
        isinstance(item.get(key), (int, float))
        and not isinstance(item.get(key), bool)
        and float(item.get(key) or 0.0) > 0
        for key in (
            "frame_sample_count",
            "frame_count",
            "average_fps",
            "one_percent_low_fps",
            "frame_p95_ms",
            "frame_p99_ms",
            "frame_issue_count",
            "frame_issue_pct",
        )
    )
    dominant = item.get("dominant_stage", {})
    dominant = dominant if isinstance(dominant, dict) else {}
    explicit_conclusion = bool(
        item.get("throttling_observed") is True
        or item.get("conclusion")
        or item.get("finding")
        or (
            dominant.get("label")
            and isinstance(dominant.get("p95_ms"), (int, float))
            and float(dominant.get("p95_ms") or 0.0) > 0
        )
    )
    return frame_evidence or explicit_conclusion


def _performance_test_item_report_rows(
    analysis: Dict[str, object],
) -> List[Dict[str, object]]:
    test_items = analysis.get("test_items", {})
    test_items = test_items if isinstance(test_items, dict) else {}
    raw_rows = test_items.get("rows", [])
    return [
        item
        for item in raw_rows
        if isinstance(item, dict) and _performance_test_item_is_substantive(item)
    ] if isinstance(raw_rows, list) else []


def _performance_test_item_optional_columns(
    analysis: Dict[str, object],
) -> Tuple[bool, bool]:
    rows = _performance_test_item_report_rows(analysis)
    show_dominant_stage = any(
        isinstance(item.get("dominant_stage"), dict)
        and bool(item["dominant_stage"].get("label"))
        and isinstance(item["dominant_stage"].get("p95_ms"), (int, float))
        for item in rows
    )
    show_thermal = any(
        isinstance(item.get("maximum_temperature_c"), (int, float))
        or isinstance(item.get("maximum_thermal_status"), (int, float))
        and float(item.get("maximum_thermal_status") or 0.0) > 0
        for item in rows
    )
    return show_dominant_stage, show_thermal


def _performance_test_item_headers(analysis: Dict[str, object]) -> str:
    show_dominant_stage, show_thermal = _performance_test_item_optional_columns(analysis)
    labels = [
        "测试项",
        "时长",
        "平均 FPS",
        "1% Low",
        "P95 / P99",
        "异常帧",
        "CPU 平均 / 峰值",
        "GPU 平均 / 峰值",
    ]
    if show_dominant_stage:
        labels.insert(6, "主要延迟阶段")
    if _performance_memory_frequency_available(analysis):
        labels.append("内存频率 平均 / P95")
    if show_thermal:
        labels.append("热限制")
    labels.extend(["测试窗口平均电池侧功率", "置信度"])
    return "".join(f"<th>{_escape(label)}</th>" for label in labels)


def _performance_test_item_rows(analysis: Dict[str, object]) -> str:
    show_memory_frequency = _performance_memory_frequency_available(analysis)
    show_dominant_stage, show_thermal = _performance_test_item_optional_columns(analysis)
    rows = []
    for item in _performance_test_item_report_rows(analysis):
        windows = item.get("windows", [])
        windows = windows if isinstance(windows, list) else []
        first_start = windows[0].get("start_elapsed_s") if windows and isinstance(windows[0], dict) else 0
        last_end = windows[-1].get("end_elapsed_s") if windows and isinstance(windows[-1], dict) else first_start
        dominant = item.get("dominant_stage", {})
        dominant = dominant if isinstance(dominant, dict) else {}
        dominant_available = bool(dominant.get("label")) and isinstance(
            dominant.get("p95_ms"), (int, float)
        )
        dominant_cell = (
            (
                f'<td>{_escape(dominant.get("label"))}'
                f'<span class="cell-sub">P95 {_number(dominant.get("p95_ms"), 2)} ms</span></td>'
                if dominant_available
                else '<td>—</td>'
            )
            if show_dominant_stage
            else ""
        )
        memory_cell = (
            f'<td>{_number(item.get("average_memory_frequency_mhz"), 0)} / '
            f'{_number(item.get("p95_memory_frequency_mhz"), 0)} MHz</td>'
            if show_memory_frequency
            else ""
        )
        thermal_cell = ""
        if show_thermal:
            temperature = item.get("maximum_temperature_c")
            thermal_status = item.get("maximum_thermal_status")
            thermal_evidence = isinstance(temperature, (int, float)) or (
                isinstance(thermal_status, (int, float))
                and float(thermal_status) > 0
            )
            temperature_detail = (
                f'<span class="cell-sub">最高 {_number(temperature)} °C</span>'
                if isinstance(temperature, (int, float))
                else ""
            )
            thermal_cell = (
                f'<td>{"是" if item.get("throttling_observed") else "否"}'
                f'{temperature_detail}</td>'
                if thermal_evidence
                else '<td>—</td>'
            )
        rows.append(
            f'<tr class="test-item-row" data-test-start="{_number(first_start, 3, "0")}" data-test-end="{_number(last_end, 3, "0")}">'
            f'<td><strong>{_escape(item.get("name"))}</strong><span class="cell-sub">{_escape(item.get("phase"))} · {int(item.get("occurrence_count") or 0)} 次</span></td>'
            f'<td>{_number(item.get("duration_s"), 1)} s</td>'
            f'<td>{_number(item.get("average_fps"), 1)} FPS</td>'
            f'<td>{_number(item.get("one_percent_low_fps"), 1)} FPS</td>'
            f'<td>{_number(item.get("frame_p95_ms"), 2)} / {_number(item.get("frame_p99_ms"), 2)} ms</td>'
            f'<td>{_number(item.get("frame_issue_pct"), 2)}%<span class="cell-sub">{int(item.get("frame_issue_count") or 0)} 帧</span></td>'
            f'{dominant_cell}'
            f'<td>{_number(item.get("average_cpu_pct"))}% / {_number(item.get("maximum_cpu_pct"))}%</td>'
            f'<td>{_number(item.get("average_gpu_load_pct"))}% / {_number(item.get("maximum_gpu_load_pct"))}%</td>'
            f'{memory_cell}'
            f'{thermal_cell}'
            f'<td>{_number(item.get("average_whole_device_power_mw"), 0)} mW<span class="cell-sub">只记录整机</span></td>'
            f'<td>{_escape(_display_label(item.get("confidence"), CONFIDENCE_LABELS))}</td>'
            "</tr>"
        )
    column_count = 13 if show_memory_frequency else 12
    return "".join(rows) or f'<tr><td colspan="{column_count}" class="empty-cell">没有可用的性能测试项区间。</td></tr>'


def _performance_test_item_span_rows(analysis: Dict[str, object]) -> str:
    test_items = analysis.get("test_items", {})
    test_items = test_items if isinstance(test_items, dict) else {}
    rows = []
    for item in test_items.get("spans", []) if isinstance(test_items.get("spans"), list) else []:
        if not isinstance(item, dict) or not _performance_test_item_is_substantive(item):
            continue
        rows.append(
            f'<tr class="test-item-row" data-test-start="{_number(item.get("start_elapsed_s"), 3, "0")}" data-test-end="{_number(item.get("end_elapsed_s"), 3, "0")}">'
            f'<td>{_number(item.get("start_elapsed_s"), 1)} s</td>'
            f'<td><strong>{_escape(item.get("name"))}</strong><span class="cell-sub">{_escape(item.get("phase"))}</span></td>'
            f'<td>{_number(item.get("duration_s"), 1)} s</td>'
            f'<td>{_number(item.get("average_fps"), 1)} FPS</td>'
            f'<td>{_number(item.get("one_percent_low_fps"), 1)} FPS</td>'
            f'<td>{_number(item.get("frame_p95_ms"), 2)} / {_number(item.get("frame_p99_ms"), 2)} ms</td>'
            f'<td>{_number(item.get("frame_issue_pct"), 2)}%</td>'
            f'<td>{_number(item.get("average_whole_device_power_mw"), 0)} mW</td>'
            f'<td>{_escape(_display_label(item.get("confidence"), CONFIDENCE_LABELS))}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="9" class="empty-cell">没有可用的单次性能测试明细。</td></tr>'


def _cpu_rows(analysis: Dict[str, object]) -> Tuple[str, str, str]:
    cpu = analysis.get("cpu", {})
    clusters = cpu.get("clusters", []) if isinstance(cpu, dict) else []
    table_rows: List[str] = []
    residency_rows: List[str] = []
    selector_buttons: List[str] = []
    for index, cluster in enumerate(clusters):
        name = str(cluster.get("name", "cluster"))
        label = _display_label(cluster.get("label", name), CLUSTER_LABELS)
        selector_buttons.append(
            f'<button type="button" class="segment-button{" active" if index == 0 else ""}" '
            f'data-cpu-cluster="{_escape(name)}">{_escape(label)}</button>'
        )
        cores = ", ".join(str(value) for value in cluster.get("cores", [])) or "—"
        table_rows.append(
            "<tr>"
            f'<td><strong>{_escape(label)}</strong><span class="cell-sub">CPU { _escape(cores) }</span></td>'
            f'<td>{_number(cluster.get("load_weighted_mhz"), 0)} MHz</td>'
            f'<td>{_number(cluster.get("maximum_mhz"), 0)} / {_number(cluster.get("hardware_max_mhz"), 0)} MHz</td>'
            "</tr>"
        )
        residency = {item.get("band"): item for item in cluster.get("residency", [])}
        low = float(residency.get("low", {}).get("load_weighted_pct") or 0.0)
        balanced = float(residency.get("balanced", {}).get("load_weighted_pct") or 0.0)
        high = max(0.0, 100.0 - low - balanced)
        residency_rows.append(
            '<div class="residency-row">'
            f'<div><strong>{_escape(label)}</strong><span>按负载加权的频率驻留</span></div>'
            '<div class="stacked-bar" role="img" '
            f'aria-label="{_escape(label)} 低频 {low:.1f}%，中频 {balanced:.1f}%，高频 {high:.1f}%">'
            f'<span class="band-low" style="width:{low:.3f}%"></span>'
            f'<span class="band-balanced" style="width:{balanced:.3f}%"></span>'
            f'<span class="band-high" style="width:{high:.3f}%"></span>'
            "</div>"
            f'<div class="residency-values"><span>L {low:.0f}%</span><span>M {balanced:.0f}%</span><span>H {high:.0f}%</span></div>'
            "</div>"
        )
    if not table_rows:
        table_rows.append('<tr><td colspan="3" class="empty-cell">CPU 核心组频率数据不可用。</td></tr>')
    return "".join(table_rows), "".join(residency_rows), "".join(selector_buttons)


def _performance_cpu_rows(analysis: Dict[str, object]) -> str:
    cpu = analysis.get("cpu", {})
    clusters = cpu.get("clusters", []) if isinstance(cpu, dict) else []
    rows = []
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        label = _display_label(cluster.get("label", cluster.get("name")), CLUSTER_LABELS)
        cores = ", ".join(str(value) for value in cluster.get("cores", [])) or "—"
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(label)}</strong><span class="cell-sub">CPU {_escape(cores)}</span></td>'
            f'<td>{_number(cluster.get("load_weighted_mhz"), 0)} MHz</td>'
            f'<td>{_number(cluster.get("maximum_mhz"), 0)} / {_number(cluster.get("hardware_max_mhz"), 0)} MHz</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="3" class="empty-cell">CPU 核心组频率数据不可用。</td></tr>'


def _process_rows(analysis: Dict[str, object]) -> str:
    rows = []
    for item in analysis.get("processes", [])[:12]:
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("name"))}</strong><span class="cell-sub">PID {_escape(item.get("pid"))}</span></td>'
            f'<td>{float(item.get("cpu_pct") or 0.0):.1f}%</td>'
            f'<td>{float(item.get("user_pct") or 0.0):.1f}%</td>'
            f'<td>{float(item.get("kernel_pct") or 0.0):.1f}%</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="4" class="empty-cell">进程 CPU 快照不可用。</td></tr>'


def _system_process_rows(analysis: Dict[str, object]) -> str:
    system = analysis.get("system", {})
    rows = []
    for item in system.get("top_processes", [])[:5] if isinstance(system, dict) else []:
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("name") or item.get("command"))}</strong>'
            f'<span class="cell-sub">{_escape(item.get("user"))} · {_escape(_display_label(item.get("category"), CATEGORY_LABELS))}</span></td>'
            f'<td>{_number(item.get("average_cpu_pct"))}%</td>'
            f'<td>{_number(item.get("average_when_visible_cpu_pct"))}%</td>'
            f'<td>{_number(item.get("maximum_cpu_pct"))}%</td>'
            f'<td>{_number(item.get("average_power_when_visible_mw"), 0)} / {_number(item.get("power_delta_when_visible_mw"), 0)} mW</td>'
            f'<td>{_number(item.get("power_correlation"), 2)}</td>'
            f'<td>{_number(item.get("average_relative_power_score"), 2)} / {_number(item.get("maximum_relative_power_score"), 2)}</td>'
            f'<td>{int(item.get("seen_snapshots") or 0)}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="8" class="empty-cell">全系统进程快照不可用。</td></tr>'


def _system_thread_rows(analysis: Dict[str, object]) -> str:
    system = analysis.get("system", {})
    rows = []
    for item in system.get("hot_threads", [])[:20] if isinstance(system, dict) else []:
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("name"))}</strong><span class="cell-sub">{_escape(item.get("process"))}</span></td>'
            f'<td>{_escape(item.get("pid"))} / {_escape(item.get("tid"))}</td>'
            f'<td>{_number(item.get("average_when_visible_cpu_pct"))}%</td>'
            f'<td>{_number(item.get("maximum_cpu_pct"))}%</td>'
            f'<td>{int(item.get("seen_snapshots") or 0)}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="5" class="empty-cell">热点线程快照不可用。</td></tr>'


def _priority_activity_content(analysis: Dict[str, object], platform: str) -> Tuple[str, str]:
    system = analysis.get("system", {})
    priority = system.get("priority_activities", {}) if isinstance(system, dict) else {}
    activity_rows = priority.get("rows", []) if isinstance(priority, dict) else []
    monitored = priority.get("monitored", []) if isinstance(priority, dict) else []
    if activity_rows:
        leading = activity_rows[0]
        delta = leading.get("power_delta_mw")
        status = (
            '<div class="priority-callout active"><span class="status-dot warning"></span><div>'
            f'<strong>检测到重点后台活动：{_escape(leading.get("label") or leading.get("name"))}</strong>'
            f'<span>估算持续 {_number(leading.get("estimated_duration_s"))} s · '
            f'相对会话基线 {_number(delta, 0)} mW · 仅表示时间相关性，不代表因果归因</span>'
            "</div></div>"
        )
    else:
        if platform == "ios":
            status = (
                '<div class="priority-callout"><span class="status-dot good"></span><div>'
                '<strong>未检测到 CPU 可见的重点系统或采集器活动</strong>'
                f'<span>已观察到 {len(monitored)} 个受监控进程；相对功耗分数与整机原始 SystemLoad 通道分开解释。</span>'
                "</div></div>"
            )
        elif platform == "harmony":
            status = (
                '<div class="priority-callout"><span class="status-dot good"></span><div>'
                '<strong>未检测到 CPU 可见的 HarmonyOS 更新、安装或编译活动</strong>'
                f'<span>已观察到 {len(monitored)} 个受监控进程；这里只报告与整机功率的时间重叠，不做进程独占归因。</span>'
                "</div></div>"
            )
        else:
            status = (
                '<div class="priority-callout"><span class="status-dot good"></span><div>'
                '<strong>未检测到 CPU 可见的 DEX 或系统更新活动</strong>'
                f'<span>已观察到 {len(monitored)} 个受监控服务；常驻 daemon 仅在 CPU 可见时标记为活动。</span>'
                "</div></div>"
            )
    rows = []
    for item in activity_rows:
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("label") or item.get("name"))}</strong><span class="cell-sub">{_escape(_display_label(item.get("kind"), ACTIVITY_KIND_LABELS))}</span></td>'
            f'<td>{int(item.get("detection_count") or 0)} / {int(item.get("window_count") or 0)}</td>'
            f'<td>{_number(item.get("estimated_duration_s"))} s</td>'
            f'<td>{_number(item.get("average_cpu_pct"))}% / {_number(item.get("maximum_cpu_pct"))}%</td>'
            f'<td>{_number(item.get("average_power_mw"), 0)} mW</td>'
            f'<td>{_number(item.get("power_delta_mw"), 0)} mW</td>'
            f'<td>{_number(item.get("excess_energy_mwh"), 3)} mWh</td>'
            f'<td>{_escape(_display_label(item.get("confidence"), CONFIDENCE_LABELS))}</td>'
            "</tr>"
        )
    table = "".join(rows) or '<tr><td colspan="8" class="empty-cell">未检测到重点后台活动窗口。</td></tr>'
    return status, table


def _activity_group_rows(analysis: Dict[str, object]) -> str:
    system = analysis.get("system", {})
    groups = system.get("activity_groups", {}) if isinstance(system, dict) else {}
    rows = []
    for item in groups.get("rows", []) if isinstance(groups, dict) else []:
        evidence = ", ".join(str(value) for value in item.get("threads", [])[:3])
        if not evidence:
            evidence = ", ".join(str(value) for value in item.get("processes", [])[:2])
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("label") or _display_label(item.get("kind"), ACTIVITY_KIND_LABELS))}</strong>'
            f'<span class="cell-sub">{_escape(item.get("subsystem"))} · {_escape(", ".join(item.get("sources", [])))}</span></td>'
            f'<td>{int(item.get("detection_count") or 0)} / {int(item.get("window_count") or 0)}</td>'
            f'<td>{_number(item.get("estimated_duration_s"))} s</td>'
            f'<td>{_number(item.get("average_cpu_pct"))}% / {_number(item.get("maximum_cpu_pct"))}%</td>'
            f'<td>{_number(item.get("average_power_mw"), 0)} / {_number(item.get("power_delta_mw"), 0)} mW</td>'
            f'<td>{_number(item.get("power_correlation"), 2)}</td>'
            f'<td>{_number(item.get("average_temperature_c"))} / {_number(item.get("maximum_temperature_c"))} °C</td>'
            f'<td>{_escape(evidence or "仅有分类证据")}</td>'
            f'<td>{_escape(_display_label(item.get("confidence"), CONFIDENCE_LABELS))}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="9" class="empty-cell">尚未在热点快照中识别到 GC、kworker、RCU、IRQ 或其他重点活动。</td></tr>'


def _interference_tag(level: object) -> str:
    key = str(level or "unknown")
    css_class = key if key in {"low", "medium", "high"} else "context"
    return (
        f'<span class="source-tag {css_class}">'
        f'{_escape(_display_label(key, INTERFERENCE_LABELS))}</span>'
    )


def _test_item_status(analysis: Dict[str, object]) -> str:
    test_items = analysis.get("test_items", {})
    if not isinstance(test_items, dict) or not test_items.get("available"):
        return (
            '<div class="availability-note"><strong>测试项数据不可用</strong>'
            '<span>导入带持续时间的 BTR2 事件后会优先按测试项分析；没有外部事件时回退到前台 Activity 区间。</span></div>'
        )
    overlap = int(test_items.get("overlap_count") or 0)
    overlap_note = (
        f"检测到 {overlap} 组重叠测试区间，重叠行不能作为独立样本直接相加。"
        if overlap
        else "测试项之间未检测到重叠区间。"
    )
    if str(test_items.get("analysis_mode") or "power") == "performance":
        return (
            '<div class="priority-callout"><span class="status-dot good"></span><div>'
            f'<strong>已按{_escape(test_items.get("source_label"))}生成 {int(test_items.get("row_count") or 0)} 个性能测试项</strong>'
            f'<span>{_escape(overlap_note)} 帧率、1% Low、P95/P99、渲染阶段、调度与热状态在测试窗口内聚合；功耗只保留整机均值。</span>'
            "</div></div>"
        )
    return (
        '<div class="priority-callout"><span class="status-dot good"></span><div>'
        f'<strong>已按{_escape(test_items.get("source_label"))}生成 {int(test_items.get("row_count") or 0)} 个测试项</strong>'
        f'<span>{_escape(overlap_note)} GC、kworker、平台后台活动与热上下文均为时间重叠证据，不是进程独占功耗。</span>'
        "</div></div>"
    )


def _test_item_rows(analysis: Dict[str, object]) -> str:
    test_items = analysis.get("test_items", {})
    platform = str(analysis.get("platform") or "android").lower()
    background_label = (
        "更新/安装/编译"
        if platform == "harmony"
        else "系统/采集器"
        if platform == "ios"
        else "DEX/更新"
    )
    thermal_label = "热严重度未公开" if platform in {"harmony", "ios"} else None
    rows = []
    for item in test_items.get("rows", []) if isinstance(test_items, dict) else []:
        gc = item.get("gc", {}) if isinstance(item.get("gc"), dict) else {}
        kworker = item.get("kworker", {}) if isinstance(item.get("kworker"), dict) else {}
        top_processes = ", ".join(
            f'{process.get("name")} {_number(process.get("average_cpu_pct"))}%'
            for process in item.get("top_processes", [])[:3]
        )
        top_activities = ", ".join(
            str(activity.get("label"))
            for activity in item.get("top_activities", [])[:3]
            if activity.get("label")
        )
        gpu_text = "—"
        if isinstance(item.get("average_gpu_load_pct"), (int, float)):
            gpu_text = (
                f'{_number(item.get("average_gpu_load_pct"))}% / '
                f'{_number(item.get("maximum_gpu_load_pct"))}%'
            )
        elif isinstance(item.get("average_gpu_frequency_mhz"), (int, float)):
            gpu_text = (
                f'{_number(item.get("average_gpu_frequency_mhz"), 0)} / '
                f'{_number(item.get("maximum_gpu_frequency_mhz"), 0)} MHz'
            )
        start_temperature = item.get("start_temperature_c")
        end_temperature = item.get("end_temperature_c")
        if isinstance(start_temperature, (int, float)) or isinstance(end_temperature, (int, float)):
            temperature_text = (
                f'{_number(start_temperature)} → {_number(end_temperature)} °C'
                f'<span class="cell-sub">传感器峰值 {_number(item.get("maximum_temperature_c"))} °C · '
                f'{_escape(thermal_label) if thermal_label else f"Thermal {int(item.get("maximum_thermal_status") or 0)}"}</span>'
            )
        else:
            temperature_text = (
                f'传感器峰值 {_number(item.get("maximum_temperature_c"))} °C'
                f'<span class="cell-sub">{_escape(thermal_label) if thermal_label else f"Thermal {int(item.get("maximum_thermal_status") or 0)}"}</span>'
            )
        rows.append(
            f'<tr class="test-item-row" data-test-start="{_number(item.get("first_start_elapsed_s"), 3, "0")}" '
            f'data-test-end="{_number(item.get("last_end_elapsed_s"), 3, "0")}">'
            f'<td><strong>{_escape(item.get("name"))}</strong><span class="cell-sub">{_escape(item.get("phase"))} · {int(item.get("count") or 0)} 次 · '
            f'{_escape(", ".join(item.get("foreground_packages", [])[:2]) or "前台未知")}</span></td>'
            f'<td>{_number(item.get("duration_s"), 1)} s<span class="cell-sub">覆盖 {_number(item.get("coverage_pct"))}%</span></td>'
            f'<td>{_number(item.get("energy_mwh"), 2)} mWh</td>'
            f'<td>{_number(item.get("mwh_per_minute"), 2)}</td>'
            f'<td>{_number(item.get("average_power_mw"), 0)} / {_number(item.get("p95_power_mw"), 0)} / {_number(item.get("maximum_power_mw"), 0)} mW</td>'
            f'<td>{_number(item.get("average_cpu_pct"))}% / {_number(item.get("maximum_cpu_pct"))}%</td>'
            f'<td>{gpu_text}</td>'
            f'<td>{temperature_text}</td>'
            f'<td>{int(gc.get("snapshot_count") or 0)} 点<span class="cell-sub">{_number(gc.get("average_cpu_pct"))}% / {_number(gc.get("maximum_cpu_pct"))}% · {_number(gc.get("overlap_s"))} s</span></td>'
            f'<td>{int(kworker.get("snapshot_count") or 0)} 点<span class="cell-sub">{_number(kworker.get("average_cpu_pct"))}% / {_number(kworker.get("maximum_cpu_pct"))}% · {_number(kworker.get("overlap_s"))} s</span></td>'
            f'<td>{_escape(background_label)} {_number(item.get("dex_update_overlap_s"))} s<span class="cell-sub">热上下文 {_number(item.get("thermal_throttling_overlap_s"))} s</span></td>'
            f'<td>{_escape(top_processes or "无进程快照")}<span class="cell-sub">{_escape(top_activities or "无重点系统活动")}</span></td>'
            f'<td>{_interference_tag(item.get("interference_level"))}<span class="cell-sub">活动重叠 {_number(item.get("system_activity_overlap_pct"))}% · 可见系统 CPU {_number(item.get("visible_system_cpu_share_pct"))}%</span></td>'
            f'<td><span class="source-tag {_escape(item.get("confidence", "low"))}">{_escape(_display_label(item.get("confidence"), CONFIDENCE_LABELS))}</span></td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="14" class="empty-cell">没有可分析的测试项区间。</td></tr>'


def _test_item_span_rows(analysis: Dict[str, object]) -> str:
    test_items = analysis.get("test_items", {})
    rows = []
    for item in (test_items.get("spans", []) if isinstance(test_items, dict) else [])[:200]:
        rows.append(
            f'<tr class="test-item-row" data-test-start="{_number(item.get("start_elapsed_s"), 3, "0")}" '
            f'data-test-end="{_number(item.get("end_elapsed_s"), 3, "0")}">'
            f'<td>{_number(item.get("start_elapsed_s"), 1)} s</td>'
            f'<td><strong>{_escape(item.get("name"))}</strong><span class="cell-sub">{_escape(item.get("phase"))}</span></td>'
            f'<td>{_number(item.get("duration_s"), 1)} s</td>'
            f'<td>{_number(item.get("energy_mwh"), 2)} mWh</td>'
            f'<td>{_number(item.get("average_power_mw"), 0)} / {_number(item.get("p95_power_mw"), 0)} / {_number(item.get("maximum_power_mw"), 0)} mW</td>'
            f'<td>{_interference_tag(item.get("interference_level"))}</td>'
            f'<td>{_escape(", ".join(item.get("foreground_packages", [])[:2]) or "未知")}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="7" class="empty-cell">没有单次测试项明细。</td></tr>'


def _thermal_sensor_rows(analysis: Dict[str, object]) -> str:
    thermal = analysis.get("thermal", {})
    rows = []
    for item in thermal.get("sensors", []) if isinstance(thermal, dict) else []:
        unit = str(item.get("unit") or "°C")
        minimum = item.get("minimum_value", item.get("minimum_c"))
        average = item.get("average_value", item.get("average_c"))
        maximum = item.get("maximum_value", item.get("maximum_c"))
        threshold_unit = unit if bool(item.get("contributes_to_thermal_status", True)) else ""
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("name"))}</strong></td>'
            f'<td>{_number(minimum)} {_escape(unit)}</td>'
            f'<td>{_number(average)} {_escape(unit)}</td>'
            f'<td>{_number(maximum)} {_escape(unit)}</td>'
            f'<td>{_escape(_display_label(item.get("maximum_status_label"), THERMAL_STATUS_LABELS))}</td>'
            f'<td>{_number(item.get("first_hot_threshold_c"))} {_escape(threshold_unit)}</td>'
            f'<td>{_number(item.get("margin_to_first_threshold_c"))} {_escape(threshold_unit)}</td>'
            f'<td>{_number(item.get("power_correlation"), 2)}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="8" class="empty-cell">热状态历史不可用，可能仅保留了测试结束时快照。</td></tr>'


def _cooling_rows(analysis: Dict[str, object]) -> str:
    thermal = analysis.get("thermal", {})
    rows = []
    for item in thermal.get("cooling_devices", []) if isinstance(thermal, dict) else []:
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("name"))}</strong></td>'
            f'<td>{_number(item.get("maximum_value"), 0)}</td>'
            f'<td>{int(item.get("active_snapshots") or 0)}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="3" class="empty-cell">系统未暴露冷却设备活动。</td></tr>'


def _brightness_throttling_section(analysis: Dict[str, object]) -> str:
    brightness = analysis.get("brightness_throttling", {})
    brightness = brightness if isinstance(brightness, dict) else {}
    if not brightness.get("available"):
        return ""
    platform = str(brightness.get("platform") or analysis.get("platform") or "android").lower()
    is_harmony = platform == "harmony"
    is_ios = platform == "ios"
    points = brightness.get("points", [])
    points = points if isinstance(points, list) else []
    current_state = brightness.get("current_state", {})
    current_state = current_state if isinstance(current_state, dict) else {}
    if not points:
        vendor_known = isinstance(current_state.get("vendor_thermal_active"), bool)
        vendor_active = current_state.get("vendor_thermal_active") is True
        vendor_last_known = isinstance(
            current_state.get("vendor_thermal_last_known_active"), bool
        )
        vendor_last_known_active = (
            current_state.get("vendor_thermal_last_known_active") is True
        )
        candidates = current_state.get("vendor_thermal_candidate_caps_nits", [])
        candidate_count = (
            len(candidates) if isinstance(candidates, (list, dict)) else 0
        )
        age_s = current_state.get("vendor_thermal_observed_age_s")
        vendor_note = None
        if vendor_last_known:
            age_text = (
                f"{_number(age_s, 1)} 秒前"
                if isinstance(age_s, (int, float))
                else "此前"
            )
            freshness = (
                "已超过低频刷新有效期，不能代表当前状态。"
                if current_state.get("vendor_thermal_state_stale")
                else "尚未到下一次低频刷新。"
            )
            vendor_note = (
                "厂商温控限亮最近一次运行时状态为 "
                f"active={'true' if vendor_last_known_active else 'false'}（{age_text}）；"
                f"{freshness}候选 nit 仍只是固件标称上限，不是亮度计实测。"
            )
        vendor_note = (
            "已识别厂商温控限亮运行时字段与候选上限表，但当前 active=false；"
            "候选 nit 只表示固件标称上限，不能证明当前档位已生效，也不能建立温度到档位的映射。"
            if vendor_note is None and vendor_known and not vendor_active and candidate_count
            else vendor_note
            or "系统亮度、DisplayManager 热亮度上限和 lcd-backlight 冷却档位未形成降亮证据。"
        )
        empty_note = (
            "逻辑亮度、设备亮度、显示折扣与 ThermalService 温度未形成联合降亮证据。"
            if is_harmony
            else "iOS 用户亮度、rawBrightness、实际毫尼特与热压力证据未形成联合降亮证据。"
            if is_ios
            else vendor_note
        )
        return (
            '<section class="analysis-section brightness-dim-section">'
            '<div class="priority-callout"><span class="status-dot good"></span><div>'
            '<strong>未观察到已生效的屏幕热降亮</strong>'
            f'<span>{_escape(empty_note)}</span>'
            '</div></div></section>'
        )
    rows = []
    for item in points:
        if not isinstance(item, dict):
            continue
        vendor_known = isinstance(item.get("vendor_thermal_active"), bool)
        status = (
            "confirmed"
            if vendor_known and item.get("vendor_thermal_active") is True
            else "suspected"
            if vendor_known
            else str(item.get("status") or "suspected")
        )
        tag = "high" if status == "confirmed" else "medium"
        label = "确认" if status == "confirmed" else "疑似"
        requested = item.get("requested_brightness")
        effective = item.get("effective_brightness")
        cap = item.get("thermal_cap")
        effective_raw = item.get("effective_raw_estimate")
        if is_ios:
            luminance = item.get("luminance_nits")
            baseline_luminance = item.get("baseline_luminance_nits")
            luminance_drop = item.get("luminance_drop_pct")
            display_text = (
                f'{_number(luminance, 1)} nits'
                if isinstance(luminance, (int, float))
                else "实际毫尼特不可读取"
            )
            if isinstance(baseline_luminance, (int, float)):
                display_text += f'<span class="cell-sub">基线 {_number(baseline_luminance, 1)} nits'
                if isinstance(luminance_drop, (int, float)):
                    display_text += f' · 下降 {_number(luminance_drop, 1)}%'
                display_text += "</span>"
        else:
            display_text = (
                f'请求 {_number(float(requested) * 100.0, 1)}% → '
                f'有效 {_number(float(effective) * 100.0, 1)}%'
                if isinstance(requested, (int, float)) and isinstance(effective, (int, float))
                else "有效亮度不可直接读取"
            )
            if isinstance(effective_raw, (int, float)):
                display_text += f'<span class="cell-sub">折算档位约 {_number(effective_raw, 0)}</span>'
            vendor_level = item.get("vendor_thermal_level")
            if (
                not is_harmony
                and item.get("vendor_thermal_active") is True
                and isinstance(vendor_level, (int, float))
            ):
                display_text += (
                    f'<span class="cell-sub">厂商运行时档位 {_number(vendor_level, 0)}</span>'
                )
        discount = item.get("brightness_discount")
        vendor_limit_nits = item.get("vendor_thermal_limit_nits")
        cap_text = (
            f'{_number(discount, 3)}×'
            if is_harmony and isinstance(discount, (int, float))
            else _escape(item.get("thermal_notification") or "无明确通知")
            if is_ios
            else (
                f'系统标称上限 {_number(vendor_limit_nits, 0)} nit'
                '<span class="cell-sub">非亮度计实测</span>'
            )
            if item.get("vendor_thermal_active") is True
            and isinstance(vendor_limit_nits, (int, float))
            else f'{_number(float(cap) * 100.0, 1)}%'
            if isinstance(cap, (int, float))
            else "—"
        )
        temperature = (
            item.get("battery_temperature_c")
            if is_ios
            else item.get("vendor_thermal_temperature_c")
            if not is_harmony
            and isinstance(item.get("vendor_thermal_temperature_c"), (int, float))
            else item.get("skin_temperature_c")
        )
        backlight_text = (
            _number(item.get("render_backlight_raw"), 0)
            if is_harmony
            else _number(item.get("raw_backlight_raw"), 0)
            if is_ios
            else _number(item.get("lcd_backlight_cooling"), 0)
        )
        temperature_source = (
            "厂商运行时"
            if not is_harmony
            and not is_ios
            and isinstance(item.get("vendor_thermal_temperature_c"), (int, float))
            else "SKIN"
            if not is_harmony and not is_ios
            else None
        )
        temperature_source_html = (
            f'<span class="cell-sub">{temperature_source}</span>'
            if temperature_source
            else ""
        )
        rows.append(
            f'<tr class="brightness-point-row" data-brightness-time="{_number(item.get("elapsed_s"), 3, "0")}">'
            f'<td>{_number(item.get("elapsed_s"), 1)} s</td>'
            f'<td><span class="source-tag {tag}">{label}</span></td>'
            f'<td>{_number(item.get("setting_raw"), 1 if is_ios else 0)}'
            f'<span class="cell-sub">{"设定未变" if item.get("setting_unchanged") else "设定可能变化"}</span></td>'
            f'<td>{display_text}</td>'
            f'<td>{cap_text}</td>'
            f'<td>{backlight_text}</td>'
            f'<td>{_number(temperature, 1)} °C'
            f'{temperature_source_html}</td>'
            f'<td>{_escape(item.get("foreground_package") or "—")}</td>'
            f'<td>{_escape(item.get("reason") or "—")}</td>'
            '</tr>'
        )
    confirmed = int(brightness.get("confirmed_point_count") or 0)
    title = "检测到屏幕热降亮" if confirmed else "检测到疑似屏幕热降亮"
    return (
        '<section class="analysis-section brightness-dim-section">'
        '<div class="chart-toolbar"><div><h2>疑似降亮度点</h2>'
        '<p class="section-copy">时间线中的橙色/红色竖线与下表逐点对应；系统设定亮度和显示侧有效亮度分开记录。</p></div>'
        f'<span class="source-tag {"high" if confirmed else "medium"}">'
        f'{int(brightness.get("point_count") or 0)} 点 / {int(brightness.get("event_count") or 0)} 段</span></div>'
        '<div class="priority-callout active"><span class="status-dot warning"></span><div>'
        f'<strong>{_escape(title)}</strong>'
        f'<span>{_escape(brightness.get("evidence_summary") or ("DisplayPowerManagerService 逻辑/设备亮度、显示折扣、RenderService 背光和 ThermalService 温度已联合判定。" if is_harmony else "AppleARMBacklight 用户亮度、rawBrightness、实际毫尼特与 iOS 热压力证据已联合判定。" if is_ios else "只有厂商运行时 active=true，或独立的 DisplayManager / Thermal HAL 明确限制证据，才标记为确认；候选表存在本身不构成确认。"))}</span>'
        '</div></div>'
        '<div class="data-table-wrap"><table><thead><tr>'
        f'<th>时间</th><th>判定</th><th>{"逻辑亮度" if is_harmony else "用户亮度 (%)" if is_ios else "系统设定"}</th><th>{"实际亮度" if is_ios else "显示侧亮度"}</th><th>{"显示折扣" if is_harmony else "热压力证据" if is_ios else "热上限"}</th>'
        f'<th>{"RS 背光" if is_harmony else "rawBrightness" if is_ios else "LCD 冷却档"}</th><th>{"外壳/系统温度" if is_harmony else "电池温度" if is_ios else "SKIN"}</th><th>前台应用</th><th>证据</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        '<div class="availability-note"><strong>测量边界</strong><span>'
        f'{_escape(brightness.get("limitations") or "无法保证绝对物理亮度；精确 nits 仍需外部亮度计。")}</span></div>'
        '</section>'
    )


def _cpuset_rows(analysis: Dict[str, object]) -> str:
    scheduler = analysis.get("scheduler", {})
    rows = []
    for item in scheduler.get("cpusets", []) if isinstance(scheduler, dict) else []:
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("name"))}</strong></td>'
            f'<td>{_escape(item.get("latest_cpus"))}</td>'
            f'<td>{_escape(", ".join(str(value) for value in item.get("observed_cpus", [])))}</td>'
            f'<td>{"是" if item.get("changed") else "否"}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="4" class="empty-cell">平台 CPU 分组 / cpuset 范围不可用。</td></tr>'


def _scheduler_policy_rows(analysis: Dict[str, object]) -> str:
    scheduler = analysis.get("scheduler", {})
    rows = []
    for item in scheduler.get("cpu_policies", []) if isinstance(scheduler, dict) else []:
        governors = ", ".join(str(value) for value in item.get("governors", [])) or "权限受限"
        related_cpus = ", ".join(str(value) for value in item.get("related_cpus", [])) or "—"
        minimum = item.get("scaling_min_khz") or item.get("cpuinfo_min_khz") or []
        maximum = item.get("scaling_max_khz") or item.get("cpuinfo_max_khz") or []
        core_min = ", ".join(str(value) for value in item.get("core_ctl_min_cpus", []))
        core_max = ", ".join(str(value) for value in item.get("core_ctl_max_cpus", []))
        core_ctl = f"{core_min or '—'}–{core_max or '—'} cores" if core_min or core_max else "不可见"
        if item.get("core_ctl_enabled") == [False]:
            core_ctl += "（关闭）"
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("name"))}</strong></td>'
            f'<td>{_escape(related_cpus)}</td>'
            f'<td>{_escape(governors)}</td>'
            f'<td>{_escape(", ".join(_number(value / 1000.0, 0) for value in minimum))} MHz</td>'
            f'<td>{_escape(", ".join(_number(value / 1000.0, 0) for value in maximum))} MHz</td>'
            f'<td>{_escape(core_ctl)}</td>'
            f'<td>{"运行时控制可见" if item.get("runtime_controls_visible") else "仅硬件范围可见"}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="7" class="empty-cell">CPU Policy 状态不可用。</td></tr>'


def _hint_session_rows(analysis: Dict[str, object]) -> str:
    scheduler = analysis.get("scheduler", {})
    rows = []
    for item in scheduler.get("hint_sessions", []) if isinstance(scheduler, dict) else []:
        flags = ", ".join(
            value
            for value, enabled in (
                ("图形管线", item.get("graphics_pipeline")),
                ("节能", item.get("power_efficient")),
                ("已暂停", item.get("force_paused")),
            )
            if enabled
        ) or "标准"
        rows.append(
            "<tr>"
            f'<td>{_escape(item.get("pid"))} / {_escape(item.get("uid"))}</td>'
            f'<td>{_escape(", ".join(str(value) for value in item.get("tids", [])))}</td>'
            f'<td>{_number(float(item.get("target_duration_ns") or 0.0) / 1_000_000.0, 3)} ms</td>'
            f'<td>{_escape(flags)}</td>'
            f'<td>{int(item.get("snapshot_count") or 0)}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="5" class="empty-cell">未观察到平台 Performance Hint / 调度会话。</td></tr>'


def _scheduler_process_rows(analysis: Dict[str, object]) -> str:
    scheduler = analysis.get("scheduler", {})
    rows = []
    for item in scheduler.get("process_states", [])[:30] if isinstance(scheduler, dict) else []:
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("name"))}</strong></td>'
            f'<td>{_escape(item.get("pid"))} / {_escape(item.get("uid"))}</td>'
            f'<td>{_escape(item.get("current_proc_state"))} · {_escape(item.get("adj_type"))}</td>'
            f'<td>{_escape(item.get("current_sched_group"))}</td>'
            f'<td>{"已冻结" if item.get("frozen") else "活动"}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="5" class="empty-cell">平台进程调度状态历史不可用。</td></tr>'


def _gpu_content(analysis: Dict[str, object], platform: str) -> Tuple[str, str, str, str]:
    gpu = analysis.get("gpu", {})
    gpu = gpu if isinstance(gpu, dict) else {}
    available = bool(gpu.get("frequency_available"))
    load_available = bool(gpu.get("load_available"))
    source = gpu.get("source", {})
    source = source if isinstance(source, dict) else {}
    model = str(gpu.get("model") or source.get("name") or "GPU")
    if available:
        status = (
            f'<span class="status-dot good"></span><span>已采集 {_escape(model)} 频率</span>'
            f'<strong>平均 {_number(gpu.get("average_frequency_mhz"), 0)} MHz</strong>'
        )
        metric = (
            '<article class="metric-card compact"><div class="metric-top"><span>GPU 频率</span>'
            '<span class="source-tag counter">计数器</span></div>'
            f'<div class="metric-value">{_number(gpu.get("average_frequency_mhz"), 0)} <small>MHz</small></div>'
            f'<div class="metric-context">峰值 {_number(gpu.get("maximum_frequency_mhz"), 0)} MHz</div></article>'
        )
    elif load_available:
        status = (
            f'<span class="status-dot good"></span><span>已采集 {_escape(model)} 负载</span>'
            f'<strong>平均 {_number(gpu.get("average_load_pct"))}%</strong>'
        )
        metric = (
            '<article class="metric-card compact"><div class="metric-top"><span>GPU 负载</span>'
            f'<span class="source-tag counter">{"DVT Graphics" if platform == "ios" else "KGSL / OEM"}</span></div>'
            f'<div class="metric-value">{_number(gpu.get("average_load_pct"))} <small>%</small></div>'
            f'<div class="metric-context">峰值 {_number(gpu.get("maximum_load_pct"))}%</div></article>'
        )
    else:
        memory = gpu.get("memory", {})
        memory = memory if isinstance(memory, dict) else {}
        if platform == "ios":
            status = (
                f'<span class="status-dot warning"></span><span>{_escape(model)} 利用率流不可用</span>'
                '<strong>未推断 GPU 电源轨功耗</strong>'
            )
        elif platform == "harmony":
            status = (
                f'<span class="status-dot warning"></span><span>{_escape(model)} 实时遥测受限</span>'
                '<strong>HarmonyOS GPU 活动明确标记为不可用</strong>'
            )
        else:
            fallback = "UID 活跃时长和内存快照" if memory.get("available") else "UID 活跃时长"
            status = (
                f'<span class="status-dot warning"></span><span>{_escape(model)} 实时节点受限</span>'
                f'<strong>已使用 {_escape(fallback)}作为回退证据</strong>'
            )
        metric = (
            '<div class="availability-note"><strong>GPU 频率/负载数据源不可用</strong>'
            f'<span>{_escape(gpu.get("unavailable_reason") or ("DVT Graphics 未返回可用事件。" if platform == "ios" else "HDC shell 无法读取 HarmonyOS GPU 频率/负载节点，且没有 Android dumpsys GPU 回退。" if platform == "harmony" else "ADB shell 无法读取 OEM GPU devfreq 节点；这通常是量产系统权限限制。"))}</span></div>'
        )
    memory = gpu.get("memory", {})
    memory = memory if isinstance(memory, dict) else {}
    if memory.get("available"):
        change = memory.get("change_bytes")
        change_text = f"变化 {_byte_size(change)}" if isinstance(change, (int, float)) else "单次快照"
        metric += (
            '<article class="metric-card compact"><div class="metric-top"><span>GPU 内存快照</span>'
            '<span class="source-tag driver">dumpsys gpu</span></div>'
            f'<div class="metric-value">{_byte_size(memory.get("end_total_bytes"))}</div>'
            f'<div class="metric-context">{_escape(change_text)}</div></article>'
        )
    rows = []
    for item in gpu.get("work_by_uid", [])[:15]:
        packages = item.get("packages") or [f"UID {item.get('uid')}"]
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(", ".join(str(value) for value in packages[:2]))}</strong><span class="cell-sub">UID {_escape(item.get("uid"))}</span></td>'
            f'<td>{float(item.get("active_ms") or 0.0):.1f} ms</td>'
            f'<td>{_number(item.get("active_ratio_pct"), 2)}%</td>'
            '<td><span class="source-tag driver">驱动</span></td>'
            "</tr>"
        )
    uid_rows = "".join(rows) or '<tr><td colspan="4" class="empty-cell">GPU 工作时长数据不可用。</td></tr>'
    memory_rows = []
    total_bytes = memory.get("end_total_bytes")
    process_memory = memory.get("processes", [])
    process_memory = process_memory if isinstance(process_memory, list) else []
    for item in process_memory[:15]:
        process_bytes = item.get("bytes")
        share = (
            float(process_bytes) / float(total_bytes) * 100.0
            if isinstance(process_bytes, (int, float))
            and isinstance(total_bytes, (int, float))
            and total_bytes > 0
            else None
        )
        memory_rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("name") or f"PID {item.get("pid")}")}</strong></td>'
            f'<td>{_escape(item.get("pid"))}</td>'
            f'<td>{_byte_size(process_bytes)}</td>'
            f'<td>{_number(share, 2)}%</td>'
            "</tr>"
        )
    gpu_memory_rows = "".join(memory_rows) or '<tr><td colspan="4" class="empty-cell">GPU 进程内存快照不可用。</td></tr>'
    return status, metric, uid_rows, gpu_memory_rows


def _component_rows(analysis: Dict[str, object]) -> str:
    components = analysis.get("components", [])
    maximum = max((float(item.get("modeled_power_mw") or 0.0) for item in components), default=1.0)
    rows = []
    for item in components[:12]:
        value = float(item.get("modeled_power_mw") or 0.0)
        width = max(0.0, min(100.0, value / maximum * 100.0))
        rows.append(
            '<div class="contributor-row">'
            f'<div><strong>{_escape(_display_label(item.get("name", "未知"), COMPONENT_LABELS))}</strong><span>{_escape(_display_label(item.get("source", "模型"), SOURCE_LABELS))}</span></div>'
            f'<div class="bar-track"><span style="width:{width:.3f}%"></span></div>'
            f'<div class="contributor-value">{value:.0f} mW</div>'
            "</div>"
        )
    return "".join(rows) or '<div class="availability-note">没有可用的组件功耗模型。</div>'


def _uid_rows(analysis: Dict[str, object]) -> str:
    rows = []
    battery_usage = analysis.get("battery_usage", {})
    for item in battery_usage.get("uids", [])[:15] if isinstance(battery_usage, dict) else []:
        packages = item.get("packages") or [item.get("token", "unknown")]
        components = ", ".join(
            f"{_display_label(key, COMPONENT_LABELS)} {float(value):.3f}"
            for key, value in list(item.get("components", {}).items())[:4]
        )
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(", ".join(str(value) for value in packages[:2]))}</strong></td>'
            f'<td>{_escape(item.get("uid") if item.get("uid") is not None else item.get("token"))}</td>'
            f'<td>{float(item.get("mah") or 0.0):.3f} mAh</td>'
            f'<td>{_escape(components or "n/a")}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="4" class="empty-cell">UID 模型数据不可用。</td></tr>'


def _wakelock_rows(analysis: Dict[str, object]) -> str:
    rows = []
    for item in analysis.get("wakelocks", [])[:12]:
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("name"))}</strong></td>'
            f'<td>{float(item.get("duration_s") or 0.0):.2f} s</td>'
            f'<td>{int(item.get("count") or 0)}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="3" class="empty-cell">未解析到内核 Wakelock。</td></tr>'


def _finding_rows(analysis: Dict[str, object]) -> str:
    return "".join(
        '<article class="finding-row">'
        f'<span class="source-tag {_escape(item.get("level", "context"))}">{_escape(_display_label(item.get("level", "info"), SOURCE_KIND_LABELS))}</span>'
        f'<div><strong>{_escape(item.get("title", "分析结论"))}</strong><p>{_escape(item.get("detail", ""))}</p></div>'
        "</article>"
        for item in analysis.get("findings", [])
    )


def _test_item_conclusion_section(
    analysis: Dict[str, object],
    test_mode: str,
) -> str:
    test_items = analysis.get("test_items", {})
    test_items = test_items if isinstance(test_items, dict) else {}
    rows = [
        item
        for item in test_items.get("rows", [])
        if isinstance(item, dict)
        and (test_mode != "performance" or _performance_test_item_is_substantive(item))
    ] if isinstance(test_items.get("rows"), list) else []
    spans = [
        item
        for item in test_items.get("spans", [])
        if isinstance(item, dict)
        and (test_mode != "performance" or _performance_test_item_is_substantive(item))
    ] if isinstance(test_items.get("spans"), list) else []
    if not rows and not spans:
        return ""
    if test_mode == "performance":
        matrix = (
            '<div class="data-table-wrap"><table><thead><tr>'
            + _performance_test_item_headers(analysis)
            + '</tr></thead><tbody>'
            + _performance_test_item_rows(analysis)
            + '</tbody></table></div>'
            if rows
            else ""
        )
        details = (
            '<div class="data-table-wrap"><table><thead><tr><th>开始时间</th><th>测试项</th>'
            '<th>时长</th><th>平均 FPS</th><th>1% Low</th><th>P95 / P99</th>'
            '<th>异常帧</th><th>平均电池侧功率</th><th>置信度</th></tr></thead><tbody>'
            + _performance_test_item_span_rows(analysis)
            + '</tbody></table></div>'
            if spans
            else ""
        )
    else:
        matrix = (
            '<div class="data-table-wrap"><table><thead><tr><th>测试项</th><th>时长</th>'
            '<th>能量</th><th>mWh/min</th><th>平均 / P95 / 峰值功率</th>'
            '<th>CPU 平均 / 峰值</th><th>GPU 平均 / 峰值</th>'
            '<th>电池起止 / 传感器峰值</th><th>GC</th><th>kworker</th>'
            '<th>平台后台活动 / 热限制</th><th>主要进程 / 活动</th>'
            '<th>系统干扰</th><th>置信度</th></tr></thead><tbody>'
            + _test_item_rows(analysis)
            + '</tbody></table></div>'
            if rows
            else ""
        )
        details = (
            '<div class="data-table-wrap"><table><thead><tr><th>开始时间</th><th>测试项</th>'
            '<th>时长</th><th>能量</th><th>平均 / P95 / 峰值功率</th>'
            '<th>系统干扰</th><th>前台应用</th></tr></thead><tbody>'
            + _test_item_span_rows(analysis)
            + '</tbody></table></div>'
            if spans
            else ""
        )
    return (
        '<section class="analysis-section"><h2>按测试项形成的结论证据</h2>'
        '<details class="evidence-details"><summary>查看测试项聚合与单次执行明细</summary>'
        + matrix
        + details
        + '</details></section>'
    )


def _analysis_conclusion_sections(
    analysis: Dict[str, object],
    test_mode: str,
) -> str:
    findings = analysis.get("findings", [])
    findings = [item for item in findings if isinstance(item, dict)] if isinstance(findings, list) else []
    summary = analysis.get("summary", {})
    summary = summary if isinstance(summary, dict) else {}
    consumption_unavailable = (
        test_mode == "power"
        and summary.get("power_valid_for_consumption") is not True
    )
    render = analysis.get("render_performance", {})
    render = render if isinstance(render, dict) else {}
    performance = analysis.get("performance", {})
    performance = performance if isinstance(performance, dict) else {}
    has_frame_evidence = test_mode == "performance" and (
        performance.get("frame_evidence_available") is True
        or bool(render.get("bottlenecks"))
        or bool(_frame_interval_values(analysis))
        or (
            isinstance(performance.get("frame_sample_count"), (int, float))
            and float(performance.get("frame_sample_count") or 0.0) > 0
        )
    )
    sections = []
    if consumption_unavailable:
        reason = str(
            summary.get("power_consumption_unavailable_reason")
            or "本次没有连续、明确未接外部电源的放电区间。"
        )
        sections.append(
            '<section class="analysis-section"><div class="priority-callout active">'
            '<span class="status-dot warning"></span><div>'
            '<strong>本次无法评价耗电或续航</strong>'
            f'<span>{_escape(reason)} 原始通道仍可查看，但不能把“没有异常结论”理解为功耗正常。</span>'
            '</div></div></section>'
        )
    if test_mode == "performance" and not has_frame_evidence:
        reason = str(
            performance.get("frame_unavailable_reason")
            or render.get("reason")
            or "本次没有取得可验证的应用帧率、逐帧间隔或渲染阶段数据。"
        )
        sections.append(
            '<section class="analysis-section"><div class="priority-callout active">'
            '<span class="status-dot warning"></span><div>'
            '<strong>本次无法评价帧表现</strong>'
            f'<span>{_escape(reason)} 原始资源数据仍可查看，但不能据此判断流畅度正常或异常。</span>'
            '</div></div></section>'
        )
    if findings:
        sections.append(
            '<section class="analysis-section"><h2>结论摘要</h2>'
            '<div class="finding-list">' + _finding_rows(analysis) + '</div></section>'
        )
    elif not consumption_unavailable and not (
        test_mode == "performance" and not has_frame_evidence
    ):
        sections.append(
            '<section class="analysis-section"><div class="priority-callout">'
            '<span class="status-dot good"></span><div>'
            '<strong>本次没有形成可独立陈述的异常结论</strong>'
            '<span>分析模块只保留证据充分的判断；请以原始数据页中的完整时间序列为准。</span>'
            '</div></div></section>'
        )

    brightness = _brightness_throttling_section(analysis)
    if brightness:
        sections.append(brightness)

    if test_mode == "performance":
        if has_frame_evidence:
            sections.append(
                '<section class="analysis-section"><h2>帧表现判断</h2>'
                + _performance_interference_status(analysis)
                + '</section>'
            )
        for section in (
            _frame_stability_section(analysis),
            _render_pipeline_section(analysis),
            _slow_frame_section(analysis),
        ):
            if section:
                sections.append(section)
        has_issue = bool(render.get("bottlenecks")) or (
            isinstance(performance.get("frame_issue_pct"), (int, float))
            and float(performance.get("frame_issue_pct") or 0.0) >= 2.0
        )
        if has_issue:
            for section in (
                _render_thread_section(analysis),
                _performance_process_section(analysis),
            ):
                if section:
                    sections.append(section)
    else:
        pressure = analysis.get("power_pressure", {})
        pressure = pressure if isinstance(pressure, dict) else {}
        memory = analysis.get("memory", {})
        memory = memory if isinstance(memory, dict) else {}
        settings = analysis.get("runtime_settings", {})
        settings = settings if isinstance(settings, dict) else {}
        if (
            memory.get("available")
            or pressure.get("drivers")
            or pressure.get("tasks")
            or int(settings.get("changed_count") or 0) > 0
        ):
            sections.append(_power_pressure_sections(analysis))
        components = analysis.get("components", [])
        components = [item for item in components if isinstance(item, dict)] if isinstance(components, list) else []
        if components:
            sections.append(
                '<section class="analysis-section"><h2>功耗贡献模型证据</h2>'
                '<details class="evidence-details"><summary>查看不可直接相加的模型贡献项</summary>'
                '<div class="contributor-list">' + _component_rows(analysis) + '</div>'
                '</details></section>'
            )

    test_items = _test_item_conclusion_section(analysis, test_mode)
    if test_items:
        sections.append(test_items)
    return "".join(sections)


def _source_rows(
    analysis: Dict[str, object],
    samples: Sequence[object] = (),
    contexts: Sequence[object] = (),
) -> str:
    render = analysis.get("render_performance", {})
    render = render if isinstance(render, dict) else {}
    pipeline = render.get("pipeline", {})
    pipeline = pipeline if isinstance(pipeline, dict) else {}
    performance = analysis.get("performance", {})
    performance = performance if isinstance(performance, dict) else {}
    scheduler = analysis.get("scheduler", {})
    scheduler = scheduler if isinstance(scheduler, dict) else {}
    thermal = analysis.get("thermal", {})
    thermal = thermal if isinstance(thermal, dict) else {}
    brightness = analysis.get("brightness_throttling", {})
    brightness = brightness if isinstance(brightness, dict) else {}
    gpu = analysis.get("gpu", {})
    gpu = gpu if isinstance(gpu, dict) else {}
    cpu = analysis.get("cpu", {})
    cpu = cpu if isinstance(cpu, dict) else {}
    memory = analysis.get("memory", {})
    memory = memory if isinstance(memory, dict) else {}
    summary = analysis.get("summary", {})
    summary = summary if isinstance(summary, dict) else {}
    applications = analysis.get("applications", {})
    applications = applications if isinstance(applications, dict) else {}
    external = analysis.get("external_events", {})
    external = external if isinstance(external, dict) else {}
    system = analysis.get("system", {})
    system = system if isinstance(system, dict) else {}
    test_items = analysis.get("test_items", {})
    test_items = test_items if isinstance(test_items, dict) else {}
    runtime_settings = analysis.get("runtime_settings", {})
    runtime_settings = runtime_settings if isinstance(runtime_settings, dict) else {}
    battery_usage = analysis.get("battery_usage", {})
    battery_usage = battery_usage if isinstance(battery_usage, dict) else {}
    power_sources = summary.get("power_sources", [])
    power_sources = power_sources if isinstance(power_sources, list) else []
    sample_rows = [item for item in samples if isinstance(item, dict)]
    context_rows = [item for item in contexts if isinstance(item, dict)]

    def numeric(value: object) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )

    def sample_has(key: str) -> bool:
        return any(numeric(item.get(key)) for item in sample_rows)

    def known_foreground(value: object) -> bool:
        text = str(value or "").strip().lower()
        return bool(text and text not in {"unknown", "none", "null", "--", "-"})

    if not power_sources:
        power_sources = sorted(
            {
                str(item.get("power_source"))
                for item in sample_rows
                if item.get("power_source")
            }
        )

    def rows_present(value: object, *keys: str) -> bool:
        if not isinstance(value, dict):
            return False
        return any(isinstance(value.get(key), list) and bool(value.get(key)) for key in keys)

    process_rows = (
        system.get("top_processes", [])
        if isinstance(system.get("top_processes"), list)
        else []
    )
    thread_rows = (
        system.get("hot_threads", [])
        if isinstance(system.get("hot_threads"), list)
        else []
    )
    app_rows = (
        applications.get("rows", [])
        if isinstance(applications.get("rows"), list)
        else []
    )
    app_transitions = (
        applications.get("transitions", [])
        if isinstance(applications.get("transitions"), list)
        else []
    )
    foreground_observed = bool(
        any(
            isinstance(item, dict) and known_foreground(item.get("package"))
            for item in [*app_rows, *app_transitions]
        )
        or known_foreground(performance.get("foreground_window_name"))
        or any(known_foreground(item.get("foreground_package")) for item in context_rows)
    )
    frame_observed = bool(
        performance.get("frame_sample_count")
        or numeric(performance.get("sampled_frame_rate_fps"))
        or numeric(performance.get("sampled_compositor_fps"))
        or performance.get("frame_rate_timeline")
    )
    refresh_observed = bool(
        numeric(performance.get("current_refresh_rate_hz"))
        or rows_present(performance, "refresh_rate_timeline", "refresh_residency")
        or sample_has("refresh_rate_hz")
        or any(numeric(item.get("refresh_rate_hz")) for item in context_rows)
    )
    cpu_usage_observed = bool(
        numeric(summary.get("average_cpu_pct")) or sample_has("cpu_pct")
    )
    cpu_frequency_observed = bool(
        any(
            isinstance(item, dict)
            and any(
                numeric(item.get(key))
                for key in ("average_mhz", "maximum_mhz", "load_weighted_mhz")
            )
            for item in (
                cpu.get("clusters", [])
                if isinstance(cpu.get("clusters"), list)
                else []
            )
        )
        or any(
            isinstance(item.get("frequencies_mhz"), dict)
            and any(
                numeric(value)
                for value in item.get("frequencies_mhz", {}).values()
            )
            for item in sample_rows
        )
    )
    cpu_model_observed = any(
        isinstance(item, dict)
        and (
            item.get("model_available") is True
            or numeric(item.get("modeled_power_mw"))
            or numeric(item.get("frequency_premium_mw"))
        )
        for item in (cpu.get("clusters", []) if isinstance(cpu.get("clusters"), list) else [])
    )
    gpu_observed = bool(
        gpu.get("frequency_available")
        or gpu.get("load_available")
        or gpu.get("work_by_uid")
        or (isinstance(gpu.get("memory"), dict) and gpu.get("memory", {}).get("available"))
        or sample_has("gpu_load_pct")
        or sample_has("gpu_frequency_mhz")
    )
    memory_observed = bool(
        memory.get("available")
        or memory.get("timeline")
        or sample_has("memory_frequency_mhz")
    )
    target_process_observed = any(
        isinstance(item, dict)
        and (
            str(item.get("source") or "") == "harmony_smartperf_target"
            or str(item.get("watch_name") or "") == "target_app"
        )
        for item in process_rows
    )
    system_process_observed = bool(
        thread_rows
        or any(
            isinstance(item, dict)
            and str(item.get("source") or "") != "harmony_smartperf_target"
            for item in process_rows
        )
    )
    relative_power_score_observed = any(
        isinstance(item, dict)
        and any(
            numeric(item.get(key))
            for key in ("average_relative_power_score", "maximum_relative_power_score")
        )
        for item in process_rows
    )
    battery_channels_observed = bool(
        (
            (
                numeric(summary.get("observed_average_current_ma"))
                or numeric(summary.get("average_current_ma"))
            )
            and numeric(summary.get("average_voltage_mv"))
        )
        or (sample_has("current_ma") and sample_has("voltage_mv"))
    )
    power_observed = bool(
        power_sources
        or any(
            numeric(summary.get(key))
            for key in (
                "observed_power_average_mw",
                "battery_flow_average_power_mw",
                "average_power_mw",
            )
        )
        or sample_has("power_mw")
    )
    thermal_observed = bool(
        thermal.get("available")
        or thermal.get("sensors")
        or thermal.get("timeline")
        or numeric(thermal.get("maximum_status"))
        or sample_has("battery_temperature_c")
    )
    battery_temperature_observed = bool(
        sample_has("battery_temperature_c")
        or any(
            isinstance(item, dict)
            and "battery" in str(item.get("name") or "").lower()
            and any(numeric(item.get(key)) for key in ("value_c", "maximum_c", "average_c"))
            for item in (
                thermal.get("sensors", [])
                if isinstance(thermal.get("sensors"), list)
                else []
            )
        )
    )
    scheduler_observed = any(
        scheduler.get(key)
        for key in ("cpusets", "cpu_policies", "hint_sessions", "process_states", "timeline")
    )
    observer_cpu_observed = bool(
        numeric(summary.get("average_collector_cpu_pct"))
        or sample_has("collector_cpu_pct")
    )
    touch_observed = numeric(performance.get("touch_interaction_count"))
    runtime_settings_observed = bool(
        runtime_settings.get("available") or rows_present(runtime_settings, "rows")
    )
    attribution_observed = bool(
        battery_usage.get("available")
        or rows_present(battery_usage, "components", "uids")
        or analysis.get("components")
    )
    test_items_observed = bool(
        test_items.get("available") or rows_present(test_items, "rows", "spans", "timeline")
    )
    external_observed = bool(
        external.get("event_count") or rows_present(external, "rows", "spans")
    )
    ios_system_load_observed = (
        "ios_power_telemetry_system_load" in {str(item) for item in power_sources}
    )

    def source_observed(metric: str) -> bool:
        if metric in {
            "Whole-device raw SystemLoad power",
            "Whole-device battery power",
        }:
            return ios_system_load_observed
        if metric == "Whole-device power recording":
            return power_observed
        if metric in {
            "Battery current and voltage",
            "Battery current, voltage and temperature",
            "Battery current",
            "Battery voltage",
        }:
            return battery_channels_observed
        if metric == "CPU utilization":
            return cpu_usage_observed
        if metric == "CPU frequency":
            return cpu_frequency_observed
        if metric == "CPU frequency impact":
            return cpu_model_observed
        if metric == "Memory frequency pressure":
            return memory_observed
        if metric == "GPU activity":
            return gpu_observed
        if metric in {
            "Foreground application",
            "Foreground application state",
            "Foreground application and screen state",
        }:
            return foreground_observed
        if metric == "Display refresh rate":
            return refresh_observed
        if metric == "Observer-related process CPU upper bound":
            return observer_cpu_observed
        if metric == "Relative process power score":
            return relative_power_score_observed
        if metric in {"System processes", "System processes and hot threads"}:
            return system_process_observed
        if metric == "Target process resources":
            return target_process_observed
        if metric == "Render and compositor thread activity":
            return bool(render.get("render_threads") or thread_rows)
        if metric == "Battery temperature":
            return battery_temperature_observed
        if metric == "Test phases and actions":
            return external_observed
        if metric in {
            "Frame rate, 1% Low and frame latency",
            "HarmonyOS application frame pacing",
            "HarmonyOS compositor cadence context",
            "Application FPS and frame jitter",
        }:
            return frame_observed
        if metric == "Render pipeline stages":
            return bool(pipeline.get("stages") or render.get("stages"))
        if metric in {"Scheduler context", "Scheduler and thermal context"}:
            return scheduler_observed
        if metric in {
            "Thermal context",
            "Thermal sensors",
            "Thermal severity, sensors and cooling devices",
        }:
            return thermal_observed
        if metric == "Brightness thermal limiting":
            return bool(brightness.get("available"))
        if metric == "Runtime settings pressure":
            return runtime_settings_observed
        if metric == "Component/app attribution":
            return attribution_observed
        if metric == "Per-test power and system interference":
            return test_items_observed
        if metric == "Delivered touch interactions":
            return touch_observed

        if metric == "iOS CPU and GPU performance context":
            return cpu_usage_observed and gpu_observed
        if metric == "CPU and process activity":
            return cpu_usage_observed and system_process_observed
        if metric == "System processes and collector overhead":
            return system_process_observed and observer_cpu_observed
        if metric == "HarmonyOS CPU/GPU/DDR and thermal context":
            return (
                cpu_usage_observed
                and cpu_frequency_observed
                and gpu_observed
                and memory_observed
                and thermal_observed
            )
        if metric == "CPU/GPU/DDR and target process resources":
            return (
                cpu_usage_observed
                and cpu_frequency_observed
                and gpu_observed
                and memory_observed
                and target_process_observed
            )
        if metric == "CPU utilization/frequency":
            return cpu_usage_observed and cpu_frequency_observed
        if metric == "Refresh-rate residency and sampled compositor frame pacing":
            return refresh_observed and frame_observed
        if metric == "Foreground window and display context":
            return foreground_observed and refresh_observed
        if metric == "Foreground window and delivered touch interactions":
            return foreground_observed and touch_observed
        if metric in {"Power and scheduler context", "cpuset, process state and ADPF hints"}:
            return scheduler_observed
        return False
    rows = []
    data_sources = analysis.get("data_sources", [])
    data_sources = data_sources if isinstance(data_sources, list) else []
    for item in data_sources:
        if not isinstance(item, dict):
            continue
        metric = str(item.get("metric") or "")
        if not source_observed(metric):
            continue
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(_display_label(item.get("metric"), METRIC_LABELS))}</strong></td>'
            f'<td>{_escape(_display_label(item.get("source"), SOURCE_LABELS))}</td>'
            f'<td><span class="source-tag {_escape(item.get("kind", "context"))}">{_escape(_display_label(item.get("kind"), SOURCE_KIND_LABELS))}</span></td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="3" class="empty-cell">本次没有形成可用于分析的数据来源。</td></tr>'


def _capture_configuration_rows(metadata: Dict[str, object]) -> str:
    platform = str(metadata.get("platform") or "android").strip().lower()
    configuration = metadata.get("capture_configuration", {})
    configuration = configuration if isinstance(configuration, dict) else {}
    rows = []
    preset = configuration.get("preset_label") or configuration.get("preset") or "旧版默认配置"
    backend = configuration.get("backend") or "platform_native"
    rows.append(
        "<tr>"
        f"<td><strong>采集预设</strong></td><td>{_escape(preset)}</td>"
        f"<td><span class=\"source-tag context\">{_escape(backend)}</span></td>"
        "<td>电流、电压与设备时间戳为基础通道，始终保留。</td>"
        "</tr>"
    )
    feature_rows = configuration.get("feature_rows", [])
    if isinstance(feature_rows, list):
        overhead_labels = {"low": "低", "medium": "中", "high": "高"}
        for item in feature_rows:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "")
            definition = CAPTURE_FEATURES.get(key, {})
            presentation = PLATFORM_FEATURE_PRESENTATION.get(platform, {}).get(key, {})
            label = (
                presentation.get("label")
                or definition.get("label")
                or item.get("label")
                or key
            )
            description = (
                presentation.get("description")
                or definition.get("description")
                or item.get("description")
                or ""
            )
            overhead = definition.get("overhead") or item.get("overhead") or "--"
            enabled = bool(item.get("enabled"))
            rows.append(
                "<tr>"
                f"<td><strong>{_escape(label)}</strong>"
                f"<span class=\"cell-sub\">{_escape(description)}</span></td>"
                f"<td><span class=\"source-tag {'measured' if enabled else 'context'}\">"
                f"{'启用' if enabled else '关闭'}</span></td>"
                f"<td>{_escape(overhead_labels.get(str(overhead), overhead))}</td>"
                f"<td>{_escape(item.get('reason') or '')}</td>"
                "</tr>"
            )
    mode = metadata.get("device_performance_mode", {})
    if isinstance(mode, dict) and mode.get("requested"):
        applied = bool(mode.get("applied"))
        restored = mode.get("restored")
        status = "已启用并恢复" if applied and restored else "已启用，恢复待确认" if applied else "启用失败"
        detail = (
            f"原模式 {mode.get('original_mode', '--')}，测试模式 602，"
            f"结束后模式 {mode.get('restored_mode', '--')}。"
        )
        rows.append(
            "<tr>"
            "<td><strong>HarmonyOS 设备高性能模式</strong>"
            "<span class=\"cell-sub\">power-shell setmode 602</span></td>"
            f"<td><span class=\"source-tag {'measured' if applied and restored else 'context'}\">{_escape(status)}</span></td>"
            "<td>设备状态变更</td>"
            f"<td>{_escape(detail)}</td>"
            "</tr>"
        )
    return "".join(rows)


def _application_rows(analysis: Dict[str, object]) -> str:
    applications = analysis.get("applications", {})
    rows = []
    for item in applications.get("rows", []) if isinstance(applications, dict) else []:
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("package"))}</strong>'
            f'<span class="cell-sub">{_escape(", ".join(item.get("activities", [])[:2]) or "无 Activity 详情")}</span></td>'
            f'<td>{_number(item.get("duration_s"), 1)} s</td>'
            f'<td>{_number(item.get("time_pct"), 1)}%</td>'
            f'<td>{_number(item.get("energy_mwh"), 2)} mWh</td>'
            f'<td>{_number(item.get("average_power_mw"), 0)} mW</td>'
            f'<td>{int(item.get("transition_count") or 0)}</td>'
            f'<td><span class="source-tag {_escape(item.get("confidence", "context"))}">{_escape(_display_label(item.get("confidence"), CONFIDENCE_LABELS))}</span></td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="7" class="empty-cell">前台应用上下文不可用。</td></tr>'


def _transition_rows(analysis: Dict[str, object]) -> str:
    applications = analysis.get("applications", {})
    rows = []
    transitions = applications.get("transitions", []) if isinstance(applications, dict) else []
    for item in transitions[:100]:
        rows.append(
            "<tr>"
            f'<td>{_number(item.get("elapsed_s"), 1)} s</td>'
            f'<td><strong>{_escape(item.get("package"))}</strong></td>'
            f'<td>{_escape(item.get("activity") or "—")}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="3" class="empty-cell">未采集到应用切换。</td></tr>'


def _phase_rows(analysis: Dict[str, object]) -> str:
    external = analysis.get("external_events", {})
    rows = []
    for item in external.get("rows", []) if isinstance(external, dict) else []:
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(item.get("name"))}</strong><span class="cell-sub">{_escape(item.get("phase"))}</span></td>'
            f'<td>{int(item.get("count") or 0)}</td>'
            f'<td>{_number(item.get("duration_s"), 1)} s</td>'
            f'<td>{_number(item.get("energy_mwh"), 2)} mWh</td>'
            f'<td>{_number(item.get("average_power_mw"), 0)} mW</td>'
            f'<td><span class="source-tag {_escape(item.get("confidence", "context"))}">{_escape(_display_label(item.get("confidence"), CONFIDENCE_LABELS))}</span></td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="6" class="empty-cell">导入带时间戳的日志后可计算阶段能耗。</td></tr>'


def _event_rows(analysis: Dict[str, object]) -> str:
    external = analysis.get("external_events", {})
    rows = []
    for item in external.get("spans", [])[:100] if isinstance(external, dict) else []:
        rows.append(
            "<tr>"
            f'<td>{_number(item.get("start_elapsed_s"), 1)} s</td>'
            f'<td><strong>{_escape(item.get("name"))}</strong><span class="cell-sub">{_escape(item.get("phase"))}</span></td>'
            f'<td>{_number(item.get("duration_s"), 1)} s</td>'
            f'<td>{_number(item.get("energy_mwh"), 2)} mWh</td>'
            f'<td><span class="source-tag {_escape(item.get("confidence", "context"))}">{_escape(_display_label(item.get("confidence"), CONFIDENCE_LABELS))}</span></td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="5" class="empty-cell">没有可用的持续事件。</td></tr>'


def _window_rows(analysis: Dict[str, object]) -> str:
    rows = []
    for item in analysis.get("long_windows", []):
        rows.append(
            "<tr>"
            f'<td>{_number(item.get("start_s"), 0)} - {_number(item.get("end_s"), 0)} s</td>'
            f'<td>{_number(item.get("covered_duration_s"), 1)} s</td>'
            f'<td>{_number(item.get("energy_mwh"), 2)} mWh</td>'
            f'<td>{_number(item.get("average_power_mw"), 0)} mW</td>'
            f'<td>{_escape(item.get("dominant_app") or "未知")}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="5" class="empty-cell">没有可用的五分钟窗口。</td></tr>'


def _finite_number(value: object) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return number
    return None


def _sample_metric_extractors(
    samples: Sequence[Dict[str, object]],
) -> Dict[str, object]:
    ios_system_load_source = "ios_power_telemetry_system_load"
    has_system_load = any(
        str(item.get("power_source") or "") == ios_system_load_source
        for item in samples
    )
    extractors: Dict[str, object] = {
        "power_mw": lambda item: (
            item.get("power_mw")
            if not has_system_load
            or str(item.get("power_source") or "") == ios_system_load_source
            else None
        ),
        "battery_flow_mw": lambda item: (
            float(item["current_ma"]) * float(item["voltage_mv"]) / 1000.0
            if _finite_number(item.get("current_ma")) is not None
            and _finite_number(item.get("voltage_mv")) is not None
            else None
        ),
        "current_ma": lambda item: item.get("current_ma"),
        "voltage_mv": lambda item: item.get("voltage_mv"),
        "cpu_pct": lambda item: item.get("cpu_pct"),
        "gpu_load_pct": lambda item: item.get("gpu_load_pct"),
        "gpu_frequency_mhz": lambda item: item.get("gpu_frequency_mhz"),
        "memory_frequency_mhz": lambda item: item.get("memory_frequency_mhz"),
        "battery_temperature_c": lambda item: item.get("battery_temperature_c"),
        "collector_cpu_pct": lambda item: item.get("collector_cpu_pct"),
        "power_sample_age_s": lambda item: item.get("power_sample_age_s"),
    }
    frequency_names = sorted(
        {
            str(name)
            for item in samples
            if isinstance(item.get("frequencies_mhz"), dict)
            for name in item.get("frequencies_mhz", {})
        }
    )
    for name in frequency_names:
        extractors[f"cpu_frequency:{name}"] = (
            lambda item, frequency_name=name: (
                item.get("frequencies_mhz", {}).get(frequency_name)
                if isinstance(item.get("frequencies_mhz"), dict)
                else None
            )
        )
    return extractors


def _lttb_series_indices(
    points: Sequence[tuple[int, float, float]],
    threshold: int,
) -> List[int]:
    count = len(points)
    if threshold >= count or threshold < 3:
        return [item[0] for item in points]
    every = (count - 2) / (threshold - 2)
    selected = [points[0][0]]
    anchor = 0
    for bucket in range(threshold - 2):
        average_start = int((bucket + 1) * every) + 1
        average_end = min(int((bucket + 2) * every) + 1, count)
        if average_start >= count:
            average_start = count - 1
        average_range = points[average_start:average_end] or [points[-1]]
        average_x = sum(item[1] for item in average_range) / len(average_range)
        average_y = sum(item[2] for item in average_range) / len(average_range)
        range_start = int(bucket * every) + 1
        range_end = min(int((bucket + 1) * every) + 1, count - 1)
        anchor_x = points[anchor][1]
        anchor_y = points[anchor][2]
        maximum_area = -1.0
        next_anchor = range_start
        for point_index in range(range_start, max(range_start + 1, range_end)):
            _, point_x, point_y = points[point_index]
            area = abs(
                (anchor_x - average_x) * (point_y - anchor_y)
                - (anchor_x - point_x) * (average_y - anchor_y)
            )
            if area > maximum_area:
                maximum_area = area
                next_anchor = point_index
        selected.append(points[next_anchor][0])
        anchor = next_anchor
    selected.append(points[-1][0])
    return selected


def _report_transition_indices(
    samples: Sequence[Dict[str, object]],
    extractors: Dict[str, object],
) -> set[int]:
    if not samples:
        return set()
    selected = {0, len(samples) - 1}

    def signature(item: Dict[str, object]) -> tuple[object, ...]:
        channel = (
            item.get("direction"),
            item.get("power_source"),
            item.get("power_valid_for_consumption"),
            item.get("external_power"),
        )
        availability = tuple(
            _finite_number(extractor(item)) is not None
            for extractor in extractors.values()
        )
        return channel + availability

    previous_signature = signature(samples[0])
    previous_elapsed = _finite_number(samples[0].get("elapsed_s")) or 0.0
    positive_intervals = [
        float(current.get("elapsed_s") or 0.0) - float(previous.get("elapsed_s") or 0.0)
        for previous, current in zip(samples, samples[1:])
        if _finite_number(current.get("elapsed_s")) is not None
        and _finite_number(previous.get("elapsed_s")) is not None
        and float(current.get("elapsed_s") or 0.0) > float(previous.get("elapsed_s") or 0.0)
    ]
    expected_interval = statistics.median(positive_intervals) if positive_intervals else 0.0
    gap_limit = max(10.0, expected_interval * 5.0)
    for index, item in enumerate(samples[1:], start=1):
        current_signature = signature(item)
        elapsed = _finite_number(item.get("elapsed_s"))
        if current_signature != previous_signature:
            selected.update({index - 1, index})
        if elapsed is not None and elapsed - previous_elapsed > gap_limit:
            selected.update({index - 1, index})
        previous_signature = current_signature
        if elapsed is not None:
            previous_elapsed = elapsed
    return selected


def _multi_metric_report_indices(
    samples: List[Dict[str, object]],
    threshold: int,
) -> tuple[List[int], List[str]]:
    count = len(samples)
    if threshold >= count or threshold < 3:
        return list(range(count)), []
    extractors = _sample_metric_extractors(samples)
    series: Dict[str, List[tuple[int, float, float]]] = {}
    for key, extractor in extractors.items():
        points = []
        for index, item in enumerate(samples):
            elapsed = _finite_number(item.get("elapsed_s"))
            value = _finite_number(extractor(item))
            if elapsed is not None and value is not None:
                points.append((index, elapsed, value))
        if len(points) >= 2:
            series[key] = points
    mandatory = _report_transition_indices(samples, extractors)
    if not series:
        step = (count - 1) / float(threshold - 1)
        return sorted({round(index * step) for index in range(threshold)} | mandatory), []
    remaining_budget = max(3 * len(series), threshold - len(mandatory))
    quota = max(3, remaining_budget // len(series))
    selected = set(mandatory)
    for points in series.values():
        selected.update(_lttb_series_indices(points, min(quota, len(points))))
    if len(selected) < threshold:
        fill_count = threshold - len(selected)
        candidates = [index for index in range(count) if index not in selected]
        if fill_count > 0 and candidates:
            selected.update(
                candidates[
                    min(
                        len(candidates) - 1,
                        int((index + 0.5) * len(candidates) / fill_count),
                    )
                ]
                for index in range(fill_count)
            )
    return sorted(selected), list(series)


def _report_metric_statistics(
    samples: Sequence[Dict[str, object]],
) -> Dict[str, Dict[str, object]]:
    result: Dict[str, Dict[str, object]] = {}
    for key, extractor in _sample_metric_extractors(samples).items():
        values = [
            value
            for item in samples
            if (value := _finite_number(extractor(item))) is not None
        ]
        if not values:
            continue
        result[key] = {
            "sample_count": len(values),
            "average": statistics.fmean(values),
            "minimum": min(values),
            "maximum": max(values),
        }
    return result


def _report_bundle(bundle: Dict[str, object], threshold: int = 1200) -> Dict[str, object]:
    prepared = copy.deepcopy(bundle)
    samples = prepared.get("samples", [])
    indices: Optional[List[int]] = None
    sampled_metrics: List[str] = []
    original_samples = samples if isinstance(samples, list) else []
    metric_statistics = (
        _report_metric_statistics(original_samples) if original_samples else {}
    )
    if isinstance(samples, list) and len(samples) > threshold:
        indices, sampled_metrics = _multi_metric_report_indices(samples, threshold)
        original_intervals = [
            float(current.get("elapsed_s") or 0.0) - float(previous.get("elapsed_s") or 0.0)
            for previous, current in zip(samples, samples[1:])
            if isinstance(previous, dict)
            and isinstance(current, dict)
            and _finite_number(previous.get("elapsed_s")) is not None
            and _finite_number(current.get("elapsed_s")) is not None
            and float(current.get("elapsed_s") or 0.0) > float(previous.get("elapsed_s") or 0.0)
        ]
        expected_interval = statistics.median(original_intervals) if original_intervals else 0.0
        gap_limit = max(10.0, expected_interval * 5.0)
        display_samples: List[Dict[str, object]] = []
        for index in indices:
            item = dict(samples[index])
            if index > 0:
                previous_elapsed = _finite_number(samples[index - 1].get("elapsed_s"))
                current_elapsed = _finite_number(samples[index].get("elapsed_s"))
                if (
                    previous_elapsed is not None
                    and current_elapsed is not None
                    and current_elapsed - previous_elapsed > gap_limit
                ):
                    item["report_break_before"] = True
            display_samples.append(item)
        prepared["samples"] = display_samples
    analysis = prepared.get("analysis", {})
    if isinstance(analysis, dict):
        cpu = analysis.get("cpu", {})
        if isinstance(cpu, dict) and indices is not None:
            timeline = cpu.get("timeline", [])
            if isinstance(timeline, list) and len(timeline) == len(samples):
                cpu["timeline"] = [timeline[index] for index in indices]
        performance = analysis.get("performance", {})
        if isinstance(performance, dict):
            frame_flow = performance.get("frame_flow", {})
            if isinstance(frame_flow, dict):
                stages = frame_flow.get("stages", [])
                if isinstance(stages, list):
                    for stage in stages:
                        if not isinstance(stage, dict):
                            continue
                        timeline = stage.get("timeline", [])
                        if not isinstance(timeline, list) or len(timeline) <= threshold:
                            continue
                        stage_points = []
                        for index, item in enumerate(timeline):
                            if not isinstance(item, dict):
                                continue
                            elapsed = _finite_number(item.get("elapsed_s"))
                            if elapsed is None:
                                elapsed = _finite_number(item.get("uptime_s"))
                            value = next(
                                (
                                    numeric
                                    for key in ("value", "frame_rate_fps", "refresh_rate_hz")
                                    if (numeric := _finite_number(item.get(key))) is not None
                                ),
                                None,
                            )
                            if elapsed is not None and value is not None:
                                stage_points.append((index, elapsed, value))
                        selected = (
                            _lttb_series_indices(stage_points, threshold)
                            if len(stage_points) > threshold
                            else list(range(len(timeline)))
                        )
                        stage["timeline"] = [timeline[index] for index in selected]
        analysis["report_payload"] = {
            "raw_sample_count": len(original_samples),
            "display_sample_count": (
                len(indices) if indices is not None else len(original_samples)
            ),
            "downsampled": indices is not None,
            "downsample_method": (
                "multi-metric stratified LTTB with source, validity, null and gap boundaries preserved"
                if indices is not None
                else "full-resolution samples"
            ),
            "sampled_metrics": sampled_metrics,
            "metric_statistics": metric_statistics,
        }
    return prepared


REPORT_FRAGMENT = r"""
<style>
  #mobile-profiler {
    --app-bg: #111315;
    --app-surface: #191c1f;
    --app-surface-2: #202429;
    --app-border: #32373d;
    --app-text: #f2f4f6;
    --app-muted: #9ca5ae;
    --series-1: #4fc3d7;
    --series-2: #72c98b;
    --series-3: #f0a15e;
    --series-4: #e46f6f;
    --series-5: #d7ca69;
    --series-6: #d87eaa;
    color: var(--app-text);
    background: var(--app-bg);
    min-width: 0;
    width: 100%;
    font-family: Inter, "Segoe UI", Arial, sans-serif;
    letter-spacing: 0;
  }
  #mobile-profiler * { box-sizing: border-box; }
  #mobile-profiler[data-test-mode="power"] [data-report-only="performance"],
  #mobile-profiler[data-test-mode="performance"] [data-report-only="power"] { display: none !important; }
  #mobile-profiler button, #mobile-profiler input { font: inherit; }
  #mobile-profiler button { letter-spacing: 0; }
  #mobile-profiler .app-topbar {
    min-height: 58px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 10px 18px;
    border-bottom: 1px solid var(--app-border);
    background: #151719;
  }
  #mobile-profiler .brand-block, #mobile-profiler .session-block,
  #mobile-profiler .device-block, #mobile-profiler .metric-top,
  #mobile-profiler .view-heading, #mobile-profiler .chart-toolbar,
  #mobile-profiler .status-line, #mobile-profiler .legend-row {
    display: flex;
    align-items: center;
  }
  #mobile-profiler .brand-block { gap: 10px; min-width: 190px; }
  #mobile-profiler .brand-mark {
    width: 26px;
    height: 26px;
    border: 1px solid var(--series-1);
    border-radius: 5px;
    display: grid;
    place-items: center;
    color: var(--series-1);
    font-weight: 500;
  }
  #mobile-profiler .brand-block strong { display: block; font-size: 15px; font-weight: 500; }
  #mobile-profiler .brand-block span, #mobile-profiler .session-block span,
  #mobile-profiler .device-block span { color: var(--app-muted); font-size: 12px; }
  #mobile-profiler .session-block { gap: 9px; min-width: 0; }
  #mobile-profiler .session-block div { min-width: 0; }
  #mobile-profiler .session-block strong, #mobile-profiler .device-block strong {
    display: block;
    font-size: 13px;
    font-weight: 500;
    overflow-wrap: anywhere;
  }
  #mobile-profiler .device-block { gap: 10px; justify-content: flex-end; text-align: right; }
  #mobile-profiler .status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--series-1); flex: 0 0 auto; }
  #mobile-profiler .status-dot.good { background: var(--series-2); }
  #mobile-profiler .status-dot.warning { background: var(--series-3); }
  #mobile-profiler .app-workspace { display: grid; grid-template-columns: 178px minmax(0, 1fr); }
  #mobile-profiler .side-tabs {
    border-right: 1px solid var(--app-border);
    padding: 14px 10px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    background: #151719;
  }
  #mobile-profiler .nav-tab {
    border: 0;
    background: transparent;
    color: var(--app-muted);
    text-align: left;
    padding: 9px 10px;
    border-radius: 5px;
    cursor: pointer;
  }
  #mobile-profiler .nav-tab:hover { background: var(--app-surface-2); color: var(--app-text); }
  #mobile-profiler .nav-tab[aria-selected="true"] { background: var(--app-surface-2); color: var(--app-text); box-shadow: inset 2px 0 0 var(--series-1); }
  #mobile-profiler .app-content { min-width: 0; padding: 22px; }
  #mobile-profiler .app-view[hidden] { display: none; }
  #mobile-profiler .app-view { display: grid; gap: 22px; min-width: 0; }
  #mobile-profiler .view-heading { justify-content: space-between; gap: 16px; align-items: flex-end; flex-wrap: wrap; }
  #mobile-profiler h1, #mobile-profiler h2, #mobile-profiler h3,
  #mobile-profiler p { margin: 0; letter-spacing: 0; }
  #mobile-profiler h1 { font-size: 22px; font-weight: 500; }
  #mobile-profiler h2 { font-size: 16px; font-weight: 500; }
  #mobile-profiler h3 { font-size: 14px; font-weight: 500; }
  #mobile-profiler .view-heading p, #mobile-profiler .section-copy,
  #mobile-profiler .metric-context, #mobile-profiler .cell-sub,
  #mobile-profiler .finding-row p, #mobile-profiler .availability-note span {
    color: var(--app-muted);
    font-size: 12px;
  }
  #mobile-profiler .metric-grid { display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 10px; }
  #mobile-profiler .metric-card {
    background: var(--app-surface);
    border: 1px solid var(--app-border);
    border-radius: 6px;
    padding: 13px 14px;
    min-width: 0;
  }
  #mobile-profiler .metric-card.compact { max-width: 260px; }
  #mobile-profiler .metric-top { justify-content: space-between; gap: 8px; color: var(--app-muted); font-size: 12px; }
  #mobile-profiler .metric-value { margin-top: 12px; font-size: 25px; font-weight: 500; white-space: nowrap; }
  #mobile-profiler .metric-value small { color: var(--app-muted); font-size: 12px; font-weight: 400; }
  #mobile-profiler .metric-context { margin-top: 5px; overflow-wrap: anywhere; }
  #mobile-profiler .raw-metric-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
  #mobile-profiler .raw-metric-card { min-width: 0; overflow: hidden; border: 1px solid var(--app-border); border-radius: 6px; background: var(--app-surface); }
  #mobile-profiler .raw-metric-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 11px 12px 7px; }
  #mobile-profiler .raw-metric-heading > div { min-width: 0; display: grid; gap: 3px; }
  #mobile-profiler .raw-metric-heading > div:last-child { flex: 0 0 auto; text-align: right; }
  #mobile-profiler .raw-metric-heading strong { font-size: 13px; font-weight: 500; overflow-wrap: anywhere; }
  #mobile-profiler .raw-metric-heading span { color: var(--app-muted); font-size: 10px; overflow-wrap: anywhere; }
  #mobile-profiler .raw-metric-heading .raw-current-value { color: var(--app-text); font-size: 14px; font-variant-numeric: tabular-nums; white-space: nowrap; }
  #mobile-profiler .raw-metric-card svg { display: block; width: 100%; min-height: 220px; }
  #mobile-profiler .raw-grid-line { stroke: var(--app-border); stroke-width: 1; }
  #mobile-profiler .raw-axis-text { fill: var(--app-muted); font-size: 10px; }
  #mobile-profiler .raw-series-line { fill: none; stroke-width: 1.8; }
  #mobile-profiler .raw-series-line.raw-only { stroke-dasharray: 6 4; opacity: .72; }
  #mobile-profiler .raw-series-line.secondary { stroke-dasharray: 5 3; stroke-width: 1.5; }
  #mobile-profiler .raw-selected-line { stroke: var(--app-muted); stroke-width: 1; }
  #mobile-profiler .raw-selected-dot { fill: var(--app-bg); stroke-width: 2; }
  #mobile-profiler .raw-excluded-window { fill: var(--series-3); opacity: .08; }
  #mobile-profiler .raw-delete-window { fill: var(--series-4); opacity: .16; pointer-events: none; }
  #mobile-profiler .report-range-window { fill: var(--series-1); opacity: .16; pointer-events: none; }
  #mobile-profiler .report-range-edge { stroke: var(--series-1); stroke-width: 1; stroke-dasharray: 4 3; pointer-events: none; }
  #mobile-profiler .time-range-brush { cursor: crosshair; touch-action: none; }
  #mobile-profiler .raw-empty { padding: 42px 16px; border: 1px solid var(--app-border); color: var(--app-muted); text-align: center; }
  #mobile-profiler .raw-sample-surface .sample-control { border-top: 0; }
  #mobile-profiler .report-range-editor { display: grid; gap: 10px; padding: 12px; border-top: 1px solid var(--app-border); }
  #mobile-profiler .report-range-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  #mobile-profiler .report-range-field { display: grid; gap: 6px; color: var(--app-muted); font-size: 11px; }
  #mobile-profiler .report-range-field > span { display: flex; justify-content: space-between; gap: 10px; }
  #mobile-profiler .report-range-field strong { color: var(--app-text); font-weight: 500; font-variant-numeric: tabular-nums; }
  #mobile-profiler .report-range-field input { width: 100%; accent-color: var(--series-1); }
  #mobile-profiler .report-range-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  #mobile-profiler .report-range-status { color: var(--app-muted); font-size: 11px; }
  #mobile-profiler .report-range-status[data-state="error"] { color: var(--series-4); }
  #mobile-profiler .report-range-summary { display: grid; gap: 9px; padding: 10px; border: 1px solid var(--app-border); border-radius: 5px; background: color-mix(in srgb, var(--app-surface) 88%, transparent); }
  #mobile-profiler .report-range-summary[hidden] { display: none; }
  #mobile-profiler .report-range-summary-heading { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; flex-wrap: wrap; }
  #mobile-profiler .report-range-summary-heading strong { font-size: 12px; font-weight: 500; }
  #mobile-profiler .report-range-summary-heading span, #mobile-profiler .report-range-summary-note { color: var(--app-muted); font-size: 10px; }
  #mobile-profiler .report-range-statistics { display: grid; grid-template-columns: minmax(150px, 1.4fr) repeat(4, minmax(72px, .7fr)); gap: 1px; overflow: auto; border: 1px solid var(--app-border); border-radius: 4px; background: var(--app-border); }
  #mobile-profiler .report-range-statistics > span { min-width: 0; padding: 6px 7px; background: var(--app-surface); color: var(--app-muted); font-size: 10px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
  #mobile-profiler .report-range-statistics > span:nth-child(-n + 5) { color: var(--app-text); font-weight: 500; }
  #mobile-profiler .report-range-statistics .range-stat-name { color: var(--app-text); }
  #mobile-profiler .delete-report-range {
    border: 1px solid color-mix(in srgb, var(--series-4) 58%, var(--app-border));
    border-radius: 5px;
    padding: 7px 11px;
    background: color-mix(in srgb, var(--series-4) 9%, transparent);
    color: var(--series-4);
    cursor: pointer;
  }
  #mobile-profiler .delete-report-range:hover:not(:disabled) { background: color-mix(in srgb, var(--series-4) 15%, transparent); }
  #mobile-profiler .delete-report-range:disabled { cursor: not-allowed; opacity: .45; }
  #mobile-profiler .source-tag {
    display: inline-flex;
    align-items: center;
    width: fit-content;
    min-height: 20px;
    padding: 2px 6px;
    border-radius: 4px;
    border: 1px solid var(--app-border);
    color: var(--app-muted);
    font-size: 11px;
    white-space: nowrap;
  }
  #mobile-profiler .source-tag.measured { color: var(--series-2); border-color: color-mix(in srgb, var(--series-2) 55%, var(--app-border)); }
  #mobile-profiler .source-tag.counter, #mobile-profiler .source-tag.driver { color: var(--series-1); border-color: color-mix(in srgb, var(--series-1) 55%, var(--app-border)); }
  #mobile-profiler .source-tag.model { color: var(--series-3); border-color: color-mix(in srgb, var(--series-3) 55%, var(--app-border)); }
  #mobile-profiler .source-tag.medium { color: var(--series-1); border-color: color-mix(in srgb, var(--series-1) 55%, var(--app-border)); }
  #mobile-profiler .source-tag.low { color: var(--series-4); border-color: color-mix(in srgb, var(--series-4) 55%, var(--app-border)); }
  #mobile-profiler .source-tag.high { color: var(--series-4); border-color: color-mix(in srgb, var(--series-4) 70%, var(--app-border)); background: rgba(228, 111, 111, .08); }
  #mobile-profiler .analysis-section { min-width: 0; border-top: 1px solid var(--app-border); padding-top: 16px; }
  #mobile-profiler .evidence-details { margin-top: 12px; border: 1px solid var(--app-border); border-radius: 6px; background: var(--app-surface); }
  #mobile-profiler .evidence-details summary { cursor: pointer; padding: 10px 12px; color: var(--app-muted); font-size: 12px; list-style-position: inside; }
  #mobile-profiler .evidence-details[open] summary { border-bottom: 1px solid var(--app-border); color: var(--app-text); }
  #mobile-profiler .evidence-details .data-table-wrap { padding: 0 10px 8px; }
  #mobile-profiler .frame-flow-visual { display: flex; align-items: stretch; gap: 8px; overflow-x: auto; padding: 2px 0 8px; }
  #mobile-profiler .frame-stage { flex: 1 0 190px; min-width: 0; padding: 13px; border: 1px solid var(--app-border); border-top-width: 3px; border-radius: 6px; background: var(--app-surface); }
  #mobile-profiler .frame-stage[data-status="primary"] { border-top-color: var(--series-2); background: rgba(114, 201, 139, .055); }
  #mobile-profiler .frame-stage[data-status="valid"] { border-top-color: var(--series-1); }
  #mobile-profiler .frame-stage[data-status="reference"] { border-top-color: var(--series-3); }
  #mobile-profiler .frame-stage[data-status="invalid"] { border-top-color: var(--series-4); background: rgba(228, 111, 111, .045); }
  #mobile-profiler .frame-stage[data-status="unavailable"] { border-top-color: var(--app-border); border-style: dashed; opacity: .78; }
  #mobile-profiler .frame-stage-top { display: flex; justify-content: space-between; gap: 8px; align-items: center; }
  #mobile-profiler .frame-stage-index { color: var(--app-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
  #mobile-profiler .frame-stage-phase { margin-top: 16px; color: var(--series-1); font-size: 11px; letter-spacing: .08em; }
  #mobile-profiler .frame-stage h3 { margin-top: 4px; min-height: 34px; }
  #mobile-profiler .frame-stage-value { margin-top: 14px; font-size: 22px; font-weight: 550; font-variant-numeric: tabular-nums; white-space: nowrap; }
  #mobile-profiler .frame-stage-caption, #mobile-profiler .frame-stage-source { color: var(--app-muted); font-size: 11px; }
  #mobile-profiler .frame-stage-source { margin-top: 12px; overflow-wrap: anywhere; }
  #mobile-profiler .frame-stage-metrics { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 10px; }
  #mobile-profiler .frame-stage-metrics span { display: grid; gap: 1px; min-width: 70px; padding: 5px 7px; background: var(--app-surface-2); }
  #mobile-profiler .frame-stage-metrics small { color: var(--app-muted); font-size: 10px; }
  #mobile-profiler .frame-stage-metrics strong { font-size: 11px; font-weight: 500; }
  #mobile-profiler .frame-flow-arrow { display: grid; place-items: center; flex: 0 0 18px; color: var(--app-muted); font-size: 18px; }
  #mobile-profiler .frame-flow-history-report { margin-top: 16px; }
  #mobile-profiler .frame-flow-history-surface svg { min-height: 340px; }
  #mobile-profiler .frame-flow-history-surface .flow-lane-label { fill: var(--app-text); font-size: 11px; font-weight: 550; }
  #mobile-profiler .frame-flow-history-surface .flow-lane-value,
  #mobile-profiler .frame-flow-history-surface .flow-lane-empty { fill: var(--app-muted); font-size: 10px; }
  #mobile-profiler .frame-flow-history-surface .flow-lane-empty { font-size: 9px; }
  #mobile-profiler .frame-flow-history-surface .flow-lane-line { fill: none; stroke-width: 2; }
  #mobile-profiler .frame-flow-history-surface .flow-lane-line.reference { stroke-dasharray: 6 4; }
  #mobile-profiler .frame-flow-history-surface .flow-lane-line.invalid { opacity: .58; stroke-dasharray: 3 4; }
  #mobile-profiler .frame-flow-history-surface .flow-lane-dot { stroke: var(--app-bg); stroke-width: 1.5; }
  #mobile-profiler .stability-metrics { display: grid; grid-template-columns: repeat(4, minmax(130px, 1fr)); gap: 8px; margin-bottom: 10px; }
  #mobile-profiler .stability-metrics > div { display: grid; gap: 5px; padding: 10px 12px; border: 1px solid var(--app-border); background: var(--app-surface); }
  #mobile-profiler .stability-metrics span { color: var(--app-muted); font-size: 11px; }
  #mobile-profiler .stability-metrics strong { font-size: 14px; font-weight: 500; font-variant-numeric: tabular-nums; }
  #mobile-profiler .frame-interval-surface svg { min-height: 300px; }
  #mobile-profiler .chart-surface .histogram-bar { fill: var(--series-1); opacity: .8; }
  #mobile-profiler .chart-surface .histogram-bar.edge { fill: var(--series-5); opacity: .86; }
  #mobile-profiler .chart-surface .histogram-bar.tail { fill: var(--series-3); opacity: .9; }
  #mobile-profiler .chart-surface .budget-line { stroke: var(--series-4); stroke-width: 1.5; stroke-dasharray: 5 4; }
  #mobile-profiler .chart-surface .budget-label { fill: var(--series-4); font-size: 11px; }
  #mobile-profiler .histogram-normal { background: var(--series-1); }
  #mobile-profiler .histogram-edge { background: var(--series-5); }
  #mobile-profiler .histogram-tail { background: var(--series-3); }
  #mobile-profiler .correlation-chart { display: grid; gap: 9px; padding: 14px; border: 1px solid var(--app-border); border-radius: 6px; background: var(--app-surface); }
  #mobile-profiler .correlation-scale { display: grid; grid-template-columns: 1fr auto 1fr; margin-left: min(220px, 28%); color: var(--app-muted); font-size: 10px; }
  #mobile-profiler .correlation-scale span:nth-child(2) { text-align: center; }
  #mobile-profiler .correlation-scale span:last-child { text-align: right; }
  #mobile-profiler .correlation-row { display: grid; grid-template-columns: minmax(125px, 220px) minmax(160px, 1fr) 52px; gap: 12px; align-items: center; }
  #mobile-profiler .correlation-label { display: grid; min-width: 0; }
  #mobile-profiler .correlation-label strong { font-size: 12px; font-weight: 500; overflow-wrap: anywhere; }
  #mobile-profiler .correlation-label span { color: var(--app-muted); font-size: 10px; }
  #mobile-profiler .correlation-track { position: relative; height: 10px; background: var(--app-surface-2); overflow: hidden; }
  #mobile-profiler .correlation-track > i { position: absolute; left: 50%; top: 0; bottom: 0; width: 1px; background: var(--app-muted); opacity: .55; z-index: 2; }
  #mobile-profiler .correlation-bar { position: absolute; top: 1px; bottom: 1px; }
  #mobile-profiler .correlation-bar.positive, #mobile-profiler .correlation-positive { background: var(--series-1); }
  #mobile-profiler .correlation-bar.negative, #mobile-profiler .correlation-negative { background: var(--series-3); }
  #mobile-profiler .correlation-value { text-align: right; font-size: 12px; font-variant-numeric: tabular-nums; }
  #mobile-profiler .chart-toolbar { justify-content: space-between; gap: 14px; flex-wrap: wrap; margin-bottom: 10px; }
  #mobile-profiler .segment-control { display: inline-flex; border: 1px solid var(--app-border); border-radius: 5px; overflow: hidden; }
  #mobile-profiler .segment-button {
    border: 0;
    border-right: 1px solid var(--app-border);
    color: var(--app-muted);
    background: transparent;
    padding: 6px 10px;
    cursor: pointer;
  }
  #mobile-profiler .segment-button:last-child { border-right: 0; }
  #mobile-profiler .segment-button:hover { color: var(--app-text); background: var(--app-surface-2); }
  #mobile-profiler .segment-button.active { background: var(--app-text); color: var(--app-bg); }
  #mobile-profiler .chart-surface {
    background: var(--app-surface);
    border: 1px solid var(--app-border);
    border-radius: 6px;
    min-width: 0;
    overflow: hidden;
  }
  #mobile-profiler .chart-surface svg { display: block; width: 100%; height: auto; min-height: 260px; }
  #mobile-profiler .chart-surface .grid { stroke: var(--app-border); stroke-width: 1; }
  #mobile-profiler .chart-surface .axis-text, #mobile-profiler .chart-surface .lane-label { fill: var(--app-muted); font-size: 11px; }
  #mobile-profiler .chart-surface .lane-value { fill: var(--app-text); font-size: 11px; }
  #mobile-profiler .chart-surface .crosshair { stroke: var(--app-muted); stroke-width: 1; }
  #mobile-profiler .chart-surface .selected-point { fill: var(--app-bg); stroke-width: 2; }
  #mobile-profiler .chart-surface .event-span { fill: var(--series-3); opacity: .12; }
  #mobile-profiler .chart-surface .event-line { stroke: var(--series-3); stroke-width: 1; }
  #mobile-profiler .chart-surface .brightness-dim-marker { stroke: var(--series-3); stroke-width: 1.4; stroke-dasharray: 4 3; opacity: .85; }
  #mobile-profiler .chart-surface .brightness-dim-marker.confirmed { stroke: var(--series-4); stroke-width: 1.8; }
  #mobile-profiler .chart-surface .brightness-dim-dot { fill: var(--series-3); stroke: var(--app-bg); stroke-width: 1.5; }
  #mobile-profiler .chart-surface .brightness-dim-dot.confirmed { fill: var(--series-4); }
  #mobile-profiler .chart-surface .app-band { opacity: .78; }
  #mobile-profiler .chart-surface .test-band { fill: var(--series-1); opacity: .28; }
  #mobile-profiler .chart-surface .activity-band { opacity: .72; }
  #mobile-profiler .chart-surface .thermal-band { fill: var(--series-4); opacity: .2; }
  #mobile-profiler .chart-surface .scheduler-band { fill: var(--series-6); opacity: .38; }
  #mobile-profiler .chart-surface .focus-window { fill: var(--series-1); opacity: .06; stroke: var(--series-1); stroke-width: 1; }
  #mobile-profiler .chart-surface .band-label { fill: var(--app-text); font-size: 11px; }
  #mobile-profiler .sample-control { padding: 10px 12px 12px; border-top: 1px solid var(--app-border); }
  #mobile-profiler .sample-control input { width: 100%; accent-color: var(--series-1); }
  #mobile-profiler .sample-detail { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; color: var(--app-muted); font-size: 12px; }
  #mobile-profiler .sample-detail strong { color: var(--app-text); font-weight: 500; }
  #mobile-profiler .split-layout { display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(280px, .8fr); gap: 22px; }
  #mobile-profiler .data-table-wrap { overflow-x: auto; max-width: 100%; }
  #mobile-profiler table { width: 100%; border-collapse: collapse; min-width: 620px; }
  #mobile-profiler th, #mobile-profiler td { border-bottom: 1px solid var(--app-border); padding: 9px 8px; text-align: left; vertical-align: middle; font-size: 12px; }
  #mobile-profiler th { color: var(--app-muted); font-weight: 400; }
  #mobile-profiler td strong { display: block; font-weight: 500; }
  #mobile-profiler .test-item-row { cursor: pointer; }
  #mobile-profiler .test-item-row:hover td { background: rgba(79, 195, 215, .055); }
  #mobile-profiler .cell-sub { display: block; margin-top: 2px; }
  #mobile-profiler .empty-cell { color: var(--app-muted); }
  #mobile-profiler .brightness-point-row td { background: rgba(240, 161, 94, .035); }
  #mobile-profiler .brightness-point-row:hover td { background: rgba(240, 161, 94, .075); }
  #mobile-profiler .finding-list { display: grid; gap: 0; }
  #mobile-profiler .finding-row { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--app-border); }
  #mobile-profiler .finding-row strong { font-size: 13px; font-weight: 500; }
  #mobile-profiler .finding-row p { margin-top: 3px; overflow-wrap: anywhere; }
  #mobile-profiler .residency-list { display: grid; gap: 15px; }
  #mobile-profiler .residency-row { display: grid; grid-template-columns: 150px minmax(180px, 1fr) 150px; gap: 12px; align-items: center; }
  #mobile-profiler .residency-row > div:first-child { display: grid; }
  #mobile-profiler .residency-row > div:first-child span { color: var(--app-muted); font-size: 11px; }
  #mobile-profiler .stacked-bar { height: 10px; display: flex; overflow: hidden; background: var(--app-surface-2); }
  #mobile-profiler .band-low { background: var(--series-2); }
  #mobile-profiler .band-balanced { background: var(--series-1); }
  #mobile-profiler .band-high { background: var(--series-3); }
  #mobile-profiler .residency-values { display: flex; justify-content: flex-end; gap: 10px; color: var(--app-muted); font-size: 11px; }
  #mobile-profiler .status-line { gap: 9px; flex-wrap: wrap; }
  #mobile-profiler .status-line strong { margin-left: auto; font-size: 12px; font-weight: 500; }
  #mobile-profiler .availability-note { border-left: 3px solid var(--series-3); padding: 8px 11px; display: grid; gap: 3px; background: var(--app-surface); }
  #mobile-profiler .availability-note strong { font-size: 13px; font-weight: 500; }
  #mobile-profiler .priority-callout { display: flex; align-items: center; gap: 11px; padding: 13px 15px; border: 1px solid var(--app-border); border-left: 3px solid var(--series-2); background: var(--app-surface); }
  #mobile-profiler .priority-callout.active { border-left-color: var(--series-3); background: rgba(240, 161, 94, .07); }
  #mobile-profiler .priority-callout div { display: grid; gap: 3px; }
  #mobile-profiler .priority-callout strong { font-size: 13px; font-weight: 550; }
  #mobile-profiler .priority-callout span { color: var(--app-muted); font-size: 11px; }
  #mobile-profiler .contributor-list { display: grid; gap: 12px; }
  #mobile-profiler .contributor-row { display: grid; grid-template-columns: minmax(130px, .8fr) minmax(180px, 2fr) 70px; gap: 12px; align-items: center; }
  #mobile-profiler .contributor-row > div:first-child { display: grid; }
  #mobile-profiler .contributor-row span { color: var(--app-muted); font-size: 11px; }
  #mobile-profiler .bar-track { height: 8px; background: var(--app-surface-2); overflow: hidden; }
  #mobile-profiler .bar-track > span { display: block; height: 100%; background: var(--series-3); }
  #mobile-profiler .contributor-value { text-align: right; font-size: 12px; }
  #mobile-profiler .warning-list { margin: 0; padding-left: 18px; color: var(--series-3); font-size: 12px; }
  #mobile-profiler .metadata-block { margin: 0; padding: 13px; background: var(--app-surface); border: 1px solid var(--app-border); border-radius: 6px; color: var(--app-muted); white-space: pre-wrap; overflow-wrap: anywhere; font-size: 11px; }
  #mobile-profiler .legend-row { gap: 14px; flex-wrap: wrap; color: var(--app-muted); font-size: 11px; }
  #mobile-profiler .legend-row span { display: inline-flex; align-items: center; gap: 5px; }
  #mobile-profiler .legend-swatch { width: 9px; height: 9px; display: inline-block; }
  @media (max-width: 980px) {
    #mobile-profiler .metric-grid { grid-template-columns: repeat(2, minmax(150px, 1fr)); }
    #mobile-profiler .split-layout { grid-template-columns: 1fr; }
    #mobile-profiler .residency-row { grid-template-columns: 130px minmax(160px, 1fr); }
    #mobile-profiler .residency-values { grid-column: 2; justify-content: flex-start; }
    #mobile-profiler .stability-metrics { grid-template-columns: repeat(2, minmax(130px, 1fr)); }
  }
  @media (max-width: 720px) {
    #mobile-profiler .app-topbar { align-items: flex-start; flex-wrap: wrap; }
    #mobile-profiler .session-block { order: 3; width: 100%; }
    #mobile-profiler .app-workspace { grid-template-columns: 1fr; }
    #mobile-profiler .side-tabs { border-right: 0; border-bottom: 1px solid var(--app-border); flex-direction: row; overflow-x: auto; padding: 8px 10px; }
    #mobile-profiler .nav-tab { flex: 0 0 auto; text-align: center; }
    #mobile-profiler .nav-tab[aria-selected="true"] { box-shadow: inset 0 -2px 0 var(--series-1); }
    #mobile-profiler .app-content { padding: 16px 12px; }
    #mobile-profiler .metric-grid { grid-template-columns: 1fr 1fr; }
    #mobile-profiler .report-range-fields { grid-template-columns: 1fr; }
    #mobile-profiler .residency-row { grid-template-columns: 1fr; }
    #mobile-profiler .residency-values { grid-column: 1; }
    #mobile-profiler .contributor-row { grid-template-columns: minmax(0, 1fr) 65px; }
    #mobile-profiler .contributor-row .bar-track { grid-column: 1 / -1; grid-row: 2; }
    #mobile-profiler .correlation-scale { margin-left: 0; }
    #mobile-profiler .correlation-row { grid-template-columns: minmax(100px, 145px) minmax(130px, 1fr) 46px; gap: 8px; }
    #mobile-profiler .raw-metric-grid { grid-template-columns: 1fr; }
  }
  @media (max-width: 440px) {
    #mobile-profiler .metric-grid { grid-template-columns: 1fr; }
    #mobile-profiler .device-block { width: 100%; justify-content: flex-start; text-align: left; }
    #mobile-profiler .metric-value { font-size: 22px; }
    #mobile-profiler .sample-detail { display: grid; grid-template-columns: 1fr; }
    #mobile-profiler .stability-metrics { grid-template-columns: 1fr; }
    #mobile-profiler .correlation-row { grid-template-columns: 1fr 46px; }
    #mobile-profiler .correlation-row .correlation-track { grid-column: 1 / -1; grid-row: 2; }
    #mobile-profiler .frame-flow-arrow { display: none; }
  }
</style>
<div id="mobile-profiler" data-test-mode="@@TEST_MODE@@">
  <header class="app-topbar">
    <div class="brand-block">
      <span class="brand-mark">M</span>
      <div><strong>Mobile Profiler</strong><span>@@REPORT_SUBTITLE@@</span></div>
    </div>
    <div class="session-block">
      <span class="status-dot good"></span>
      <div><strong>@@TITLE@@</strong><span>@@TARGET@@ | @@DURATION@@ s | @@SAMPLES@@ 个样本</span></div>
    </div>
    <div class="device-block">
      <span class="status-dot"></span>
      <div><strong>@@DEVICE@@</strong><span>@@PLATFORM_NAME@@ @@OS_VERSION@@ | @@HARDWARE@@</span></div>
    </div>
  </header>
  <div class="app-workspace">
    <nav class="side-tabs" role="tablist" aria-label="报告主要部分">
      <button type="button" class="nav-tab" role="tab" aria-selected="true" data-view="raw">原始数据</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="analysis">分析结论</button>
    </nav>
    <main class="app-content">
      <section class="app-view" data-panel="raw">
        <div class="view-heading"><div><h1>原始数据随时间变化</h1><p>只展示本次已启用并实际采到有效样本的指标；所有图表共享同一时间轴并同时呈现。</p></div><span class="source-tag measured" id="raw-metric-count">等待数据</span></div>
        <div class="availability-note" id="raw-sampling-note" hidden><strong>图表显示经过保峰抽样</strong><span></span></div>
        <div class="chart-surface raw-sample-surface">
          <div class="sample-control">
            <input id="overview-slider" type="range" min="0" max="@@SLIDER_MAX@@" value="0" aria-label="选择所有原始图表的同一时间点">
            <div class="sample-detail" id="sample-detail" aria-live="polite"></div>
          </div>
          <div class="report-range-editor" aria-label="查看或删除报告时间段">
            <div class="report-range-fields">
              <label class="report-range-field" for="report-range-start"><span>选区起点（滑块微调） <strong id="report-range-start-value">--</strong></span><input id="report-range-start" type="range" min="0" max="@@SLIDER_MAX@@" value="0"></label>
              <label class="report-range-field" for="report-range-end"><span>选区终点（滑块微调） <strong id="report-range-end-value">--</strong></span><input id="report-range-end" type="range" min="0" max="@@SLIDER_MAX@@" value="0"></label>
            </div>
            <div class="report-range-summary" id="report-range-summary" hidden>
              <div class="report-range-summary-heading"><strong id="report-range-summary-title">选区统计</strong><span id="report-range-summary-time">--</span></div>
              <div class="report-range-statistics" id="report-range-statistics" aria-live="polite"></div>
              <p class="report-range-summary-note" id="report-range-summary-note"></p>
            </div>
            <div class="report-range-actions"><span class="report-range-status" id="report-range-status" aria-live="polite">在任意时间图上横向拖动框选，也可用上方滑块微调。</span><button class="delete-report-range" id="delete-report-range" type="button" disabled>删除选中时间段记录</button></div>
          </div>
        </div>
        <div class="raw-metric-grid" id="raw-metric-grid" aria-live="polite"></div>
        @@FRAME_FLOW_HISTORY_SECTION@@
        <details class="evidence-details"><summary>查看采集配置与数据来源</summary>
          <div class="data-table-wrap"><table><thead><tr><th>项目</th><th>状态</th><th>干扰等级</th><th>原因 / 恢复状态</th></tr></thead><tbody>@@CAPTURE_CONFIGURATION_ROWS@@</tbody></table></div>
          <div class="data-table-wrap"><table><thead><tr><th>指标</th><th>来源</th><th>类型</th></tr></thead><tbody>@@SOURCE_ROWS@@</tbody></table></div>
        </details>
        @@WARNING_SECTION@@
      </section>

      <section class="app-view" data-panel="analysis" hidden>
        <div class="view-heading"><div><h1>分析结论</h1><p>只展示能够由本次有效数据支持的判断与必要证据；无数据或证据不足的模块不会占用页面。</p></div><span class="source-tag counter">证据驱动</span></div>
        @@ANALYSIS_CONCLUSION_SECTIONS@@
        @@ANALYSIS_COVERAGE_SECTION@@
      </section>
    </main>
  </div>
</div>
<script>
(() => {
  const root = document.getElementById("mobile-profiler");
  const bundle = @@DATA@@;
  const metadata = bundle.metadata || {};
  const samples = bundle.samples || [];
  const contexts = (bundle.contexts || []).slice().sort((a, b) => Number(a.uptime_s) - Number(b.uptime_s));
  const events = (bundle.events || []).slice().sort((a, b) => Number(a.device_uptime_s) - Number(b.device_uptime_s));
  const analysis = bundle.analysis || {};
  const summary = analysis.summary || {};
  const reportPayload = analysis.report_payload || {};
  const reportMetricStatistics = reportPayload.metric_statistics || {};
  const cpu = analysis.cpu || { clusters: [], timeline: [] };
  const gpu = analysis.gpu || {};
  const performance = analysis.performance || {};
  const thermal = analysis.thermal || {};
  const frameFlow = performance.frame_flow || {};
  const brightnessDim = analysis.brightness_throttling || {};
  const brightnessDimPoints = (brightnessDim.points || []).slice().sort((a, b) => Number(a.elapsed_s) - Number(b.elapsed_s));
  const brightnessTimeline = (brightnessDim.timeline || []).slice().sort((a, b) => Number(a.elapsed_s) - Number(b.elapsed_s));
  const thermalTimeline = (thermal.timeline || []).slice().sort((a, b) => Number(a.elapsed_s) - Number(b.elapsed_s));
  const testMode = root.dataset.testMode || "power";
  const frameTimeline = (performance.frame_rate_timeline || []).slice().sort((a, b) => Number(a.uptime_s) - Number(b.uptime_s));
  const refreshTimeline = (performance.refresh_rate_timeline || []).slice().sort((a, b) => Number(a.uptime_s || a.elapsed_s) - Number(b.uptime_s || b.elapsed_s));
  const captureConfiguration = metadata.capture_configuration || {};
  const captureFeatures = captureConfiguration.features || {};
  const reportPlatform = String(metadata.platform || "").toLowerCase();
  const reportBackend = String(captureConfiguration.backend || "");
  const smartPerfThermal = reportPlatform === "harmony" && reportBackend === "harmony_smartperf";
  const thermalTelemetrySource = smartPerfThermal
    ? "HarmonyOS SmartPerf SP_daemon 温度字段"
    : reportPlatform === "harmony"
      ? "HarmonyOS ThermalService · hidumper"
      : reportPlatform === "ios"
        ? "iOS DiagnosticsService"
        : "Android ThermalService / Thermal HAL";
  const batteryTemperatureSource = smartPerfThermal
    ? "HarmonyOS SmartPerf SP_daemon 温度字段"
    : reportPlatform === "harmony"
      ? "HarmonyOS BatteryService · hidumper"
      : reportPlatform === "ios"
        ? "iOS DiagnosticsService"
        : "Android BatteryService";
  const testItems = analysis.test_items || { rows: [], spans: [], instant_events: [] };
  const reportEdits = metadata.report_edits || {};
  const reportExcludedRanges = Array.isArray(reportEdits.excluded_ranges) ? reportEdits.excluded_ranges : [];
  const reportRunName = (() => {
    const match = window.location.pathname.match(/\/runs\/([^/]+)\/report\.html$/);
    if (!match) return "";
    try { return decodeURIComponent(match[1]); } catch (_error) { return ""; }
  })();
  const colors = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)"];
  let selectedIndex = 0;
  let selectedCluster = cpu.clusters.length ? cpu.clusters[0].name : null;
  let testRange = null;
  let rawMetrics = [];
  let reportRangeTouched = false;
  let reportRangeStartElapsed = 0;
  let reportRangeEndElapsed = 0;
  let reportRangeSourceKey = "";
  let reportRangeFullSummary = null;
  let reportRangeSummaryError = "";
  let reportRangeSummaryTimer = null;
  let reportRangeSummaryRequestId = 0;

  function svgNode(name, attrs = {}, text = "") {
    const node = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (text) node.textContent = text;
    return node;
  }
  function finite(value) { return value != null && Number.isFinite(Number(value)); }
  function clusterLabel(cluster) {
    const labels = { Little: "小核", Middle: "中核", Big: "大核", Performance: "性能核", Prime: "超大核" };
    return labels[cluster.label] || cluster.label || cluster.name || "CPU";
  }
  function cpuCoreGroupLabel(cluster) {
    const cores = (Array.isArray(cluster && cluster.cores) ? cluster.cores : [])
      .map(Number)
      .filter(Number.isFinite)
      .sort((left, right) => left - right);
    if (!cores.length) return clusterLabel(cluster);
    const contiguous = cores.every((core, index) => index === 0 || core === cores[index - 1] + 1);
    if (contiguous && cores.length > 1) return `CPU${cores[0]}–${cores.at(-1)}`;
    return cores.map(core => `CPU${core}`).join(" / ");
  }
  function format(value, unit) {
    if (!finite(value)) return "n/a";
    const number = Number(value);
    const digits = Math.abs(number) >= 100 ? 0 : Math.abs(number) >= 10 ? 1 : 2;
    return `${number.toFixed(digits)} ${unit}`;
  }
  function formatTime(value) {
    const seconds = Math.max(0, Number(value) || 0);
    if (seconds < 120) return `${seconds.toFixed(0)}s`;
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remaining = Math.floor(seconds % 60);
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remaining).padStart(2, "0")}` : `${minutes}:${String(remaining).padStart(2, "0")}`;
  }
  function formatExactTime(value) {
    const seconds = Math.max(0, Number(value) || 0);
    if (seconds < 120) return `${seconds.toFixed(2).replace(/\.00$/, "").replace(/(\.\d)0$/, "$1")}s`;
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remaining = seconds % 60;
    const tail = remaining.toFixed(1).padStart(4, "0");
    return hours
      ? `${hours}:${String(minutes).padStart(2, "0")}:${tail}`
      : `${minutes}:${tail}`;
  }
  function chartWidth(svg) {
    return Math.max(360, Math.round(svg.getBoundingClientRect().width || 1080));
  }
  function maxTime() { return Math.max(1, ...samples.map(sample => Number(sample.elapsed_s || 0))); }
  function appendBrightnessMarkers(svg, x, top, bottom, rangeStart = 0, rangeEnd = maxTime()) {
    brightnessDimPoints.forEach(point => {
      const elapsed = Number(point.elapsed_s || 0);
      if (elapsed < rangeStart || elapsed > rangeEnd) return;
      const status = String(point.status || "suspected");
      const xPos = x(elapsed);
      const line = svgNode("line", {
        x1: xPos,
        x2: xPos,
        y1: top,
        y2: bottom,
        class: `brightness-dim-marker ${status}`,
      });
      line.appendChild(svgNode("title", {}, `${formatTime(elapsed)} · ${status === "confirmed" ? "确认热降亮" : "疑似热降亮"} · ${point.reason || "显示侧热限制"}`));
      svg.appendChild(line);
      svg.appendChild(svgNode("circle", {
        cx: xPos,
        cy: top + 6,
        r: status === "confirmed" ? 4.5 : 3.5,
        class: `brightness-dim-dot ${status}`,
      }));
    });
  }
  function sessionStartUptime() {
    if (finite(reportEdits.time_origin_uptime_s)) return Number(reportEdits.time_origin_uptime_s);
    return samples.length ? Number(samples[0].uptime_s || 0) : 0;
  }
  function reportExcludedElapsedRanges() {
    const origin = sessionStartUptime();
    return reportExcludedRanges.map(range => ({
      start: finite(range.start_elapsed_s) ? Number(range.start_elapsed_s) : Number(range.start_uptime_s) - origin,
      end: finite(range.end_elapsed_s) ? Number(range.end_elapsed_s) : Number(range.end_uptime_s) - origin,
    })).filter(range => finite(range.start) && finite(range.end) && range.end > range.start);
  }
  function crossesReportExcludedRange(previousElapsed, currentElapsed) {
    return reportExcludedElapsedRanges().some(range => (
      Number(previousElapsed) <= range.start && Number(currentElapsed) >= range.end
    ));
  }
  function selectedReportRange() {
    if (!reportRangeTouched) return null;
    const startElapsed = Math.min(reportRangeStartElapsed, reportRangeEndElapsed);
    const endElapsed = Math.max(reportRangeStartElapsed, reportRangeEndElapsed);
    if (!finite(startElapsed) || !finite(endElapsed) || endElapsed <= startElapsed) return null;
    const origin = sessionStartUptime();
    return {
      startElapsed,
      endElapsed,
      startUptime: origin + startElapsed,
      endUptime: origin + endElapsed,
      duration: endElapsed - startElapsed,
      sourceKey: reportRangeSourceKey,
    };
  }
  function contextForUptime(uptime) {
    let selected = null;
    for (const context of contexts) {
      if (Number(context.uptime_s) > Number(uptime)) break;
      selected = context;
    }
    return selected;
  }
  function frameForUptime(uptime) {
    let selected = null;
    for (const frame of frameTimeline) {
      if (Number(frame.uptime_s) > Number(uptime)) break;
      selected = frame;
    }
    return selected;
  }
  function featureEnabled(name) {
    const keys = Object.keys(captureFeatures);
    return !keys.length || captureFeatures[name] === true;
  }
  function elapsedForPoint(point) {
    if (finite(point && point.elapsed_s)) return Math.max(0, Number(point.elapsed_s));
    if (finite(point && point.uptime_s)) return Math.max(0, Number(point.uptime_s) - sessionStartUptime());
    return null;
  }
  function metricPoints(rows, value, {
    pointMeta = null,
    stale = null,
    elapsed = null,
    collapseSteps = false,
  } = {}) {
    const ordered = (Array.isArray(rows) ? rows : [])
      .map((row, index) => {
        const observedElapsed = elapsedForPoint(row);
        const resolvedElapsed = typeof elapsed === "function" ? elapsed(row, observedElapsed) : observedElapsed;
        return { row, index, elapsed: resolvedElapsed, observedElapsed };
      })
      .filter(item => item.elapsed != null && item.observedElapsed != null)
      .sort((left, right) => left.elapsed - right.elapsed || left.index - right.index);
    const points = [];
    let breakPending = false;
    let breakElapsed = null;
    ordered.forEach(item => {
      const explicitBreak = Boolean(
        item.row?.report_break_before
        || item.row?._report_break_before
        || item.row?.break_before
      );
      const staleValue = typeof stale === "function" && stale(item.row);
      const next = value(item.row);
      if (explicitBreak || staleValue || !finite(next)) {
        breakPending = true;
        if (breakElapsed == null) breakElapsed = item.observedElapsed;
        if (!finite(next) || staleValue) return;
      }
      const metadata = typeof pointMeta === "function" ? pointMeta(item.row) : null;
      const previousPoint = points.at(-1);
      if (
        collapseSteps
        && previousPoint
        && !breakPending
        && !explicitBreak
        && Math.abs(Number(previousPoint.value) - Number(next)) <= Math.max(.01, Math.abs(Number(next)) * .0001)
        && Boolean(previousPoint.rawOnly) === Boolean(metadata?.rawOnly)
      ) return;
      points.push({
        elapsed: Number(item.elapsed),
        value: Number(next),
        breakBefore: breakPending || explicitBreak,
        breakElapsed: breakPending ? breakElapsed : null,
        ...(metadata && typeof metadata === "object" ? metadata : {}),
      });
      breakPending = false;
      breakElapsed = null;
    });
    if (breakPending && points.length && breakElapsed != null) {
      points.at(-1).breakAfterElapsed = breakElapsed;
    }
    return points;
  }
  function metricGapLimit(points, sampleSeries = false) {
    const intervals = [];
    for (let index = 1; index < points.length; index += 1) {
      const previous = points[index - 1];
      const point = points[index];
      if (!point.breakBefore && point.elapsed > previous.elapsed) {
        intervals.push(point.elapsed - previous.elapsed);
      }
    }
    intervals.sort((left, right) => left - right);
    const median = intervals.length ? intervals[Math.floor(intervals.length / 2)] : 0;
    const configured = finite(metadata.sample_interval_s) ? Number(metadata.sample_interval_s) : 0;
    if (sampleSeries && reportPayload.downsampled) {
      return Number.POSITIVE_INFINITY;
    }
    return Math.max(3, median * 4, configured * 4);
  }
  function reportPowerChannelPresentation() {
    const sources = Array.from(new Set(samples.map(sample => String(sample.power_source || "")).filter(Boolean)));
    const externalPowerObserved = samples.some(sample => sample.external_power === true);
    const systemLoad = sources.includes("ios_power_telemetry_system_load");
    if (systemLoad) {
      return {
        label: "iOS 整机原始 SystemLoad 通道",
        source: "DiagnosticsService PowerTelemetryData.SystemLoad；外供时可能接近 SystemPowerIn，非电池 I×V / 非独立电源轨实测",
        step: true,
        batteryFlowNeeded: true,
        systemLoad: true,
      };
    }
    if (String(metadata.platform || "").toLowerCase() === "ios") {
      return {
        label: "iOS 电池流量功率（电流×电压）",
        source: `${sources[0] || "ios_battery_current_voltage"}；只表示电池端流量，不冒充 SystemLoad 或设备独立电源轨`,
        step: false,
        batteryFlowNeeded: false,
        systemLoad: false,
      };
    }
    if (sources.length > 1) {
      return {
        label: "原始功率通道",
        source: `${sources.join(" / ")}；按来源原样展示，虚线区段不进入耗电结论`,
        step: false,
        batteryFlowNeeded: true,
        systemLoad: false,
      };
    }
    if (externalPowerObserved) {
      return {
        label: "电池侧原始流量（含外部供电区间）",
        source: `${sources[0] || "电池电流×电压"}；external_power=true 的区段仅原样展示，不进入耗电结论`,
        step: false,
        batteryFlowNeeded: false,
        systemLoad: false,
      };
    }
    return {
      label: "电池侧原始功率",
      source: `${sources[0] || "电池电流×电压"}；虚线区段不进入耗电结论`,
      step: false,
      batteryFlowNeeded: false,
      systemLoad: false,
    };
  }
  function reportPrimaryPowerValue(sample, channel = reportPowerChannelPresentation()) {
    if (channel.systemLoad && sample?.power_source !== "ios_power_telemetry_system_load") return null;
    return sample?.power_mw;
  }
  function standardOnePercentLowAvailable() {
    const source = String(performance.one_percent_low_source || "").toLowerCase();
    const detailedIntervals = frameTimeline.some(frame => (
      Array.isArray(frame.frame_intervals_ms) && frame.frame_intervals_ms.length > 0
    ));
    const detailedSource = ["slowest 1%", "frame-time histogram", "frame-jitter"].some(token => source.includes(token));
    return detailedIntervals || (detailedSource && !source.includes("sampled-window") && !source.includes("counter-window"));
  }
  function rawMetricDefinitions() {
    const metrics = [];
    const add = definition => {
      const features = Array.isArray(definition.features)
        ? definition.features
        : definition.feature ? [definition.feature] : [];
      if (features.length && !features.some(featureEnabled)) return;
      const points = (definition.points || [])
        .filter(point => point && finite(point.elapsed) && finite(point.value))
        .map(point => ({ ...point, elapsed: Number(point.elapsed), value: Number(point.value) }))
        .sort((left, right) => left.elapsed - right.elapsed);
      if (!points.length) return;
      if (definition.requiresPositive && !points.some(point => point.value > 0)) return;
      const overlays = (Array.isArray(definition.overlays) ? definition.overlays : [])
        .map(overlay => ({
          ...overlay,
          points: (overlay.points || [])
            .filter(point => point && finite(point.elapsed) && finite(point.value))
            .map(point => ({ ...point, elapsed: Number(point.elapsed), value: Number(point.value) }))
            .sort((left, right) => left.elapsed - right.elapsed),
        }))
        .filter(overlay => overlay.points.length);
      metrics.push({
        ...definition,
        points,
        overlays,
        gapLimit: definition.gapLimit ?? metricGapLimit(points, definition.sampleSeries === true),
      });
    };
    const sampleMetric = (definition, value) => add({
      ...definition,
      sampleSeries: true,
      points: metricPoints(samples, value, definition),
    });
    const frameMetric = (definition, value) => add({
      ...definition,
      feature: "frame_rate",
      points: metricPoints(frameTimeline, value, definition),
    });

    const powerChannel = reportPowerChannelPresentation();
    sampleMetric({
      key: "power_mw",
      statisticsKey: "power_mw",
      label: powerChannel.label,
      unit: "mW",
      source: powerChannel.source,
      color: colors[0],
      step: powerChannel.step,
      carryForward: powerChannel.systemLoad,
      gapLimit: powerChannel.systemLoad ? Number.POSITIVE_INFINITY : undefined,
      collapseSteps: powerChannel.systemLoad,
      elapsed: powerChannel.systemLoad
        ? sample => {
            const observedElapsed = elapsedForPoint(sample);
            return observedElapsed == null
              ? null
              : finite(sample.power_sample_age_s)
                ? Math.max(0, observedElapsed - Math.max(0, Number(sample.power_sample_age_s)))
                : observedElapsed;
          }
        : null,
      stale: powerChannel.systemLoad
        ? sample => finite(sample.power_sample_age_s) && Number(sample.power_sample_age_s) > 30
        : null,
      summaryValue: summary.observed_power_average_mw,
      summaryMaximum: summary.observed_power_maximum_mw,
      summaryLabel: "时间加权均值",
      pointMeta: sample => ({
        rawOnly: sample.power_valid_for_consumption !== true,
        powerSource: sample.power_source || "",
      }),
    }, sample => reportPrimaryPowerValue(sample, powerChannel));
    if (powerChannel.batteryFlowNeeded) {
      sampleMetric({
        key: "battery_flow_mw",
        statisticsKey: "battery_flow_mw",
        label: "电池流量功率（电流×电压）",
        unit: "mW",
        source: "电池电流幅值 × 电池电压；与 iOS SystemLoad 分域展示",
        color: colors[1],
        summaryValue: summary.battery_flow_average_power_mw,
        summaryMaximum: summary.battery_flow_maximum_power_mw,
        summaryLabel: "时间加权均值",
        pointMeta: sample => ({ rawOnly: sample.power_valid_for_consumption !== true }),
      }, sample => finite(sample.current_ma) && finite(sample.voltage_mv)
        ? Number(sample.current_ma) * Number(sample.voltage_mv) / 1000
        : null);
    }
    sampleMetric({ key: "current_ma", label: "电池电流幅值", unit: "mA", source: "逐样本方向与有符号电流见同时间点详情", color: colors[1], summaryValue: summary.observed_average_current_ma, summaryMaximum: summary.observed_maximum_current_ma, summaryLabel: "时间加权均值" }, sample => sample.current_ma);
    sampleMetric({ key: "voltage_mv", label: "电压", unit: "mV", source: "电池基础通道", color: colors[4], requiresPositive: true }, sample => sample.voltage_mv);
    sampleMetric({ key: "cpu_pct", label: "CPU 整体负载", unit: "%", source: "整机利用率", color: colors[2], feature: "cpu_usage" }, sample => sample.cpu_pct);

    const frequencyGroups = Array.isArray(cpu.clusters) && cpu.clusters.length
      ? cpu.clusters
      : Array.from(new Set(samples.flatMap(sample => Object.keys(sample.frequencies_mhz || {}))))
        .map(name => ({ name, label: name, cores: [] }));
    frequencyGroups.forEach((cluster, index) => {
      const harmonyFrequency = String(metadata.platform || "").toLowerCase() === "harmony";
      const frequencyCadence = Number(metadata.sampling_schedule_s && metadata.sampling_schedule_s.cpu_frequency || 0);
      sampleMetric({
        key: `cpu_frequency:${cluster.name}`,
        label: `${cpuCoreGroupLabel(cluster)} ${harmonyFrequency ? "分组频率均值" : "共享频率"}`,
        unit: "MHz",
        source: `${clusterLabel(cluster)} · ${cluster.name || (harmonyFrequency ? "maximum-frequency group" : "cpufreq policy")}${harmonyFrequency && frequencyCadence > 1.5 ? ` · 约 ${frequencyCadence.toFixed(0)} 秒刷新，中间点保持最近值` : ""}`,
        color: colors[(index + 3) % colors.length],
        feature: "cpu_frequency",
        requiresPositive: true,
      }, sample => (sample.frequencies_mhz || {})[cluster.name]);
    });

    const frameRatePoints = metricPoints(frameTimeline, frame => frame.frame_rate_fps);
    const onePercentLowPoints = standardOnePercentLowAvailable()
      ? metricPoints(frameTimeline, frame => frame.one_percent_low_fps)
      : [];
    const onePercentLowTimelineLabel = performance.one_percent_low_timeline_label || "1% Low";
    add({
      key: "frame_rate_fps",
      label: onePercentLowPoints.length ? `主帧率与 ${onePercentLowTimelineLabel}` : "主帧率",
      unit: "FPS",
      source: onePercentLowPoints.length
        ? `${performance.frame_source || "前台帧计数"} · ${onePercentLowTimelineLabel}：${performance.one_percent_low_timeline_source || performance.one_percent_low_source || "逐帧间隔"}`
        : performance.frame_source || "前台帧计数",
      color: colors[0],
      feature: "frame_rate",
      requiresPositive: true,
      summaryValue: performance.sampled_frame_rate_fps,
      summaryLabel: "全程平均帧率",
      points: frameRatePoints,
      overlays: onePercentLowPoints.length ? [{
        key: "one_percent_low_fps",
        label: onePercentLowTimelineLabel,
        color: colors[4],
        points: onePercentLowPoints,
      }] : [],
    });
    frameMetric({ key: "frame_time_p95_ms", label: "帧耗时 P95", unit: "ms", source: "逐窗口帧间隔", color: colors[3], requiresPositive: true, summaryValue: performance.frame_metric_p95_ms, summaryLabel: "全程 P95", statisticKind: "quantile" }, frame => frame.frame_time_p95_ms);
    frameMetric({ key: "frame_time_p99_ms", label: "帧耗时 P99", unit: "ms", source: "逐窗口帧间隔", color: colors[3], requiresPositive: true, summaryValue: performance.frame_metric_p99_ms, summaryLabel: "全程 P99", statisticKind: "quantile" }, frame => frame.frame_time_p99_ms);
    frameMetric({ key: "frame_issue_pct", label: "异常帧比例", unit: "%", source: performance.frame_issue_label || "截止时间 / VSync", color: colors[3], summaryValue: performance.frame_issue_pct, summaryLabel: "总体异常占比", statisticKind: "ratio" }, frame => frame.frame_issue_pct);

    add({
      key: "refresh_rate_hz",
      label: "屏幕刷新率",
      unit: "Hz",
      source: performance.refresh_rate_timeline_source || performance.refresh_residency_source || "显示模式",
      color: colors[5],
      features: ["foreground_window", "frame_rate"],
      requiresPositive: true,
      step: true,
      carryForward: true,
      points: metricPoints(refreshTimeline, point => finite(point.value) ? point.value : point.refresh_rate_hz),
    });

    const primaryFrameStageKey = String(frameFlow.primary_key || "");
    (Array.isArray(frameFlow.stages) ? frameFlow.stages : []).forEach((stage, index) => {
      const stageKey = String(stage.key || index);
      if (stageKey === primaryFrameStageKey || stageKey === "display_scanout") return;
      add({
        key: `frame_stage:${stageKey}`,
        label: `链路节点 · ${stage.phase || "STAGE"} ${stage.label || stage.key || index + 1}`,
        unit: stage.timeline_unit || stage.unit || "FPS",
        source: stage.source || "渲染链路计数",
        color: colors[index % colors.length],
        feature: "frame_rate",
        requiresPositive: true,
        step: stage.key === "display_scanout",
        points: metricPoints(stage.timeline, point => finite(point.value)
          ? point.value
          : finite(point.frame_rate_fps) ? point.frame_rate_fps : point.refresh_rate_hz),
      });
    });

    sampleMetric({ key: "gpu_load_pct", label: "GPU 负载", unit: "%", source: "GPU 计数器", color: colors[1], feature: "gpu_metrics" }, sample => sample.gpu_load_pct);
    sampleMetric({ key: "gpu_frequency_mhz", label: "GPU 频率", unit: "MHz", source: "GPU 频率节点", color: colors[5], feature: "gpu_metrics", requiresPositive: true }, sample => sample.gpu_frequency_mhz);
    sampleMetric({ key: "memory_frequency_mhz", label: "内存频率", unit: "MHz", source: "DMC / DRAM / MIF", color: colors[4], feature: "memory_frequency", requiresPositive: true }, sample => sample.memory_frequency_mhz);
    sampleMetric({ key: "battery_temperature_c", label: "电池温度", unit: "°C", source: batteryTemperatureSource, color: colors[3], feature: "thermal" }, sample => sample.battery_temperature_c);
    sampleMetric({ key: "collector_cpu_pct", label: "观察者相关进程 CPU 上界", unit: "%", source: "sysmond / DTServiceHub / pairing daemon 同期 CPU；不是工具净开销", color: colors[2], feature: "cpu_usage" }, sample => sample.collector_cpu_pct);
    sampleMetric({ key: "power_sample_age_s", label: "iOS SystemLoad 样本年龄", unit: "s", source: "距 DiagnosticsService SystemLoad 最近一次变化的时间", color: colors[5] }, sample => sample.power_sample_age_s);

    const sensorRows = Array.isArray(thermal.sensors) ? thermal.sensors : [];
    const temperatureSensors = sensorRows.filter(sensor => {
      const name = String(sensor?.name || "");
      const type = Number(sensor?.type);
      const unit = String(sensor?.unit || "").trim().toLowerCase();
      if (/battery/i.test(name) || type === 2) return false;
      if (sensor?.contributes_to_thermal_status === false || [6, 7, 8].includes(type)) return false;
      return sensor?.contributes_to_thermal_status === true
        || ["°c", "c", "celsius"].includes(unit);
    });
    const thermalSensorGroups = [
      { key: "cpu", label: "CPU 最高温", types: [0] },
      { key: "gpu", label: "GPU 最高温", types: [1] },
      { key: "skin", label: "机身 / Skin 温度", types: [3] },
      { key: "usb", label: "USB 端口最高温", types: [4] },
      { key: "power-amplifier", label: "功放最高温", types: [5] },
      { key: "npu", label: "NPU 最高温", types: [9] },
    ];
    const groupedTemperatureNames = new Set();
    thermalSensorGroups.forEach((group, index) => {
      const names = temperatureSensors
        .filter(sensor => group.types.includes(Number(sensor?.type)))
        .map(sensor => String(sensor?.name || ""))
        .filter(Boolean);
      if (!names.length) return;
      names.forEach(name => groupedTemperatureNames.add(name));
      add({
        key: `thermal_sensor_group:${group.key}`,
        label: group.label,
        unit: "°C",
        source: `${thermalTelemetrySource} · ${names.length} 个同类传感器取最高值`,
        color: colors[(index + 3) % colors.length],
        feature: "thermal",
        points: metricPoints(thermalTimeline, point => {
          const values = names.map(name => (point.sensors || {})[name]).filter(finite).map(Number);
          return values.length ? Math.max(...values) : null;
        }),
      });
    });
    const otherTemperatureNames = temperatureSensors
      .map(sensor => String(sensor?.name || ""))
      .filter(name => name && !groupedTemperatureNames.has(name));
    if (otherTemperatureNames.length) {
      add({
        key: "thermal_sensor_group:other",
        label: "其他有效温度最高值",
        unit: "°C",
        source: `${thermalTelemetrySource} · ${otherTemperatureNames.length} 个未分类温度传感器取最高值`,
        color: colors[2],
        feature: "thermal",
        points: metricPoints(thermalTimeline, point => {
          const values = otherTemperatureNames.map(name => (point.sensors || {})[name]).filter(finite).map(Number);
          return values.length ? Math.max(...values) : null;
        }),
      });
    }
    add({
      key: "thermal_status",
      label: "热状态等级",
      unit: "级",
      source: thermalTelemetrySource,
      color: colors[3],
      feature: "thermal",
      step: true,
      carryForward: true,
      points: metricPoints(thermalTimeline, point => point.status),
    });

    add({
      key: "requested_brightness_pct",
      label: "系统请求亮度",
      unit: "%",
      source: "DisplayManager",
      color: colors[4],
      features: ["foreground_window", "thermal"],
      points: metricPoints(brightnessTimeline, point => finite(point.requested_brightness) ? Number(point.requested_brightness) * 100 : null),
    });
    add({
      key: "effective_brightness_pct",
      label: "显示侧有效亮度",
      unit: "%",
      source: "DisplayManager / Thermal HAL",
      color: colors[3],
      features: ["foreground_window", "thermal"],
      points: metricPoints(brightnessTimeline, point => finite(point.effective_brightness) ? Number(point.effective_brightness) * 100 : null),
    });
    add({
      key: "brightness_thermal_cap_pct",
      label: "亮度热上限",
      unit: "%",
      source: "DisplayManager thermal cap",
      color: colors[3],
      feature: "thermal",
      points: metricPoints(brightnessTimeline, point => finite(point.thermal_cap) ? Number(point.thermal_cap) * 100 : null),
    });
    const foregroundCategories = [];
    const foregroundCategoryIndex = new Map();
    const foregroundPoints = [];
    let previousForeground = null;
    contexts.forEach(context => {
      const elapsed = elapsedForPoint(context);
      const packageName = String(context.foreground_package || "").trim();
      if (elapsed == null || !packageName || packageName === previousForeground) return;
      if (!foregroundCategoryIndex.has(packageName)) {
        foregroundCategoryIndex.set(packageName, foregroundCategories.length);
        foregroundCategories.push(packageName);
      }
      foregroundPoints.push({
        elapsed,
        value: foregroundCategoryIndex.get(packageName),
        label: [packageName, context.foreground_activity].filter(Boolean).join(" · "),
      });
      previousForeground = packageName;
    });
    add({
      key: "foreground_application_state",
      label: "前台应用状态",
      unit: "",
      source: "实际收到的前台应用 / Ability 状态事件",
      color: colors[2],
      features: ["foreground_window"],
      step: true,
      carryForward: true,
      categories: foregroundCategories,
      points: foregroundPoints,
    });
    return metrics;
  }
  function rawValueText(metric, point) {
    if (!point) return "n/a";
    if (Array.isArray(metric.categories) && metric.categories.length) {
      return point.label || metric.categories[Math.round(point.value)] || "n/a";
    }
    return format(point.value, metric.unit);
  }
  function elapsedInsideReportExclusion(elapsed) {
    return reportExcludedElapsedRanges().some(range => elapsed >= range.start && elapsed <= range.end);
  }
  function rawSeriesPointAt(points, metric, elapsed) {
    if (elapsedInsideReportExclusion(elapsed)) return null;
    let selected = null;
    let following = null;
    for (const point of points) {
      if (point.elapsed > elapsed) { following = point; break; }
      selected = point;
    }
    if (!selected) return null;
    if (finite(selected.breakAfterElapsed) && elapsed >= Number(selected.breakAfterElapsed)) return null;
    if (
      following?.breakBefore
      && finite(following.breakElapsed)
      && elapsed >= Number(following.breakElapsed)
    ) return null;
    const gapLimit = Number(metric.gapLimit || 3);
    if (!metric.carryForward && elapsed - selected.elapsed > gapLimit) return null;
    if (
      following
      && following.elapsed - selected.elapsed > gapLimit
      && elapsed > selected.elapsed + gapLimit
    ) return null;
    return selected;
  }
  function rawMetricPointAt(metric, elapsed) {
    return rawSeriesPointAt(metric.points, metric, elapsed);
  }
  function rawMetricSegments(points, metric) {
    const segments = [];
    points.forEach(point => {
      const segment = segments.at(-1);
      const previous = segment?.at(-1);
      const startsNew = !segment
        || point.breakBefore
        || (previous && point.elapsed - previous.elapsed > Number(metric.gapLimit || 3))
        || (previous && crossesReportExcludedRange(previous.elapsed, point.elapsed))
        || (previous && Boolean(previous.rawOnly) !== Boolean(point.rawOnly));
      if (startsNew) segments.push([]);
      segments.at(-1).push(point);
    });
    return segments;
  }
  function rawMaximumTime() {
    return Math.max(
      maxTime(),
      ...rawMetrics.flatMap(metric => [
        ...metric.points.map(point => point.elapsed),
        ...(metric.overlays || []).flatMap(overlay => overlay.points.map(point => point.elapsed)),
      ]),
    );
  }
  function renderRawMetricChart(metric) {
    const svg = metric.svg;
    if (!svg || !metric.points.length || !samples.length) return;
    const width = chartWidth(svg), height = 220;
    const categorical = Array.isArray(metric.categories) && metric.categories.length;
    const margin = { left: categorical ? (width < 440 ? 96 : 132) : width < 440 ? 54 : 62, right: 14, top: 16, bottom: 32 };
    const maximumTime = rawMaximumTime();
    const allSeriesPoints = [metric.points, ...(metric.overlays || []).map(overlay => overlay.points)].flat();
    const observedMaximum = Math.max(0, ...allSeriesPoints.map(point => point.value));
    const maximumValue = categorical
      ? Math.max(1, metric.categories.length - 1)
      : Math.max(1, observedMaximum * 1.08);
    const x = value => margin.left + Math.max(0, Math.min(maximumTime, Number(value))) / maximumTime * (width - margin.left - margin.right);
    const y = value => margin.top + (maximumValue - Math.max(0, Number(value))) / maximumValue * (height - margin.top - margin.bottom);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("aria-label", `${metric.label}随时间变化，纵轴从 0 ${metric.unit} 开始`);
    svg.replaceChildren();
    svg.appendChild(svgNode("title", {}, `${metric.label}随时间变化`));
    for (let tick = 0; tick <= 3; tick++) {
      const ratio = tick / 3;
      const value = maximumValue * (1 - ratio);
      const yPos = margin.top + ratio * (height - margin.top - margin.bottom);
      svg.appendChild(svgNode("line", { x1: margin.left, x2: width - margin.right, y1: yPos, y2: yPos, class: "raw-grid-line" }));
      const axisLabel = categorical
        ? metric.categories[Math.max(0, Math.min(metric.categories.length - 1, Math.round(value)))] || "0"
        : format(value, metric.unit);
      svg.appendChild(svgNode("text", { x: margin.left - 7, y: yPos + 3, "text-anchor": "end", class: "raw-axis-text" }, axisLabel));
    }
    for (let tick = 0; tick <= 4; tick++) {
      const elapsed = maximumTime * tick / 4;
      const xPos = x(elapsed);
      svg.appendChild(svgNode("line", { x1: xPos, x2: xPos, y1: margin.top, y2: height - margin.bottom, class: "raw-grid-line" }));
      svg.appendChild(svgNode("text", { x: xPos, y: height - 10, "text-anchor": tick === 0 ? "start" : tick === 4 ? "end" : "middle", class: "raw-axis-text" }, formatTime(elapsed)));
    }
    reportExcludedElapsedRanges().forEach(range => {
      const startX = x(range.start);
      const endX = x(range.end);
      svg.appendChild(svgNode("rect", {
        x: startX,
        y: margin.top,
        width: Math.max(1, endX - startX),
        height: height - margin.top - margin.bottom,
        class: "raw-excluded-window",
      }));
    });
    const drawSeries = (seriesPoints, { color, secondary = false, step = metric.step } = {}) => {
      const coordinates = seriesPoints.map(point => ({ ...point, x: x(point.elapsed), y: y(point.value) }));
      const segments = rawMetricSegments(coordinates, metric);
      segments.forEach((segment, segmentIndex) => {
        if (segment.length === 1) {
          svg.appendChild(svgNode("circle", {
            cx: segment[0].x,
            cy: segment[0].y,
            r: secondary ? 2.7 : 3.5,
            fill: color,
            class: segment[0].rawOnly ? "raw-only" : "",
          }));
          return;
        }
        let path = `M${segment[0].x.toFixed(2)},${segment[0].y.toFixed(2)}`;
        for (let index = 1; index < segment.length; index += 1) {
          const previous = segment[index - 1];
          const current = segment[index];
          path += step
            ? ` L${current.x.toFixed(2)},${previous.y.toFixed(2)} L${current.x.toFixed(2)},${current.y.toFixed(2)}`
            : ` L${current.x.toFixed(2)},${current.y.toFixed(2)}`;
        }
        if (
          step
          && metric.carryForward
          && segmentIndex === segments.length - 1
          && segment.at(-1).elapsed < maximumTime
        ) {
          path += ` L${x(maximumTime).toFixed(2)},${segment.at(-1).y.toFixed(2)}`;
        }
        const classNames = [
          "raw-series-line",
          secondary ? "secondary" : "",
          segment[0].rawOnly ? "raw-only" : "",
        ].filter(Boolean).join(" ");
        svg.appendChild(svgNode("path", { d: path, class: classNames, stroke: color }));
      });
    };
    drawSeries(metric.points, { color: metric.color });
    (metric.overlays || []).forEach(overlay => drawSeries(overlay.points, {
      color: overlay.color,
      secondary: true,
      step: overlay.step === true,
    }));
    const selectedElapsed = Number(samples[selectedIndex].elapsed_s || 0);
    const selectedPoint = rawMetricPointAt(metric, selectedElapsed);
    const selectedX = x(selectedElapsed);
    svg.appendChild(svgNode("line", { x1: selectedX, x2: selectedX, y1: margin.top, y2: height - margin.bottom, class: "raw-selected-line" }));
    if (selectedPoint) {
      svg.appendChild(svgNode("circle", { cx: x(selectedPoint.elapsed), cy: y(selectedPoint.value), r: 3.5, class: "raw-selected-dot", stroke: metric.color }));
      const values = [rawValueText(metric, selectedPoint)];
      (metric.overlays || []).forEach(overlay => {
        const overlayPoint = rawSeriesPointAt(overlay.points, metric, selectedElapsed);
        if (overlayPoint) values.push(`${overlay.label} ${format(overlayPoint.value, metric.unit)}`);
      });
      metric.valueNode.textContent = values.join(" · ");
    } else {
      metric.valueNode.textContent = "n/a";
    }
    attachTimeRangeBrush(svg, {
      width,
      height,
      left: margin.left,
      right: margin.right,
      top: margin.top,
      bottom: margin.bottom,
      rangeStart: 0,
      rangeEnd: maximumTime,
      sourceKey: metric.key,
      hoverSelect: false,
    });
  }
  function renderRawMetricCharts() {
    rawMetrics.forEach(renderRawMetricChart);
  }
  function renderRawMetricGrid() {
    const container = root.querySelector("#raw-metric-grid");
    const count = root.querySelector("#raw-metric-count");
    const samplingNote = root.querySelector("#raw-sampling-note");
    if (!container) return;
    rawMetrics = rawMetricDefinitions();
    container.replaceChildren();
    if (count) count.textContent = `${rawMetrics.length} 项有效时间序列`;
    if (samplingNote) {
      samplingNote.hidden = !reportPayload.downsampled;
      const detail = samplingNote.querySelector("span");
      if (detail && reportPayload.downsampled) {
        detail.textContent = `原始 ${Number(reportPayload.raw_sample_count || 0).toLocaleString("zh-CN")} 点，图表显示 ${Number(reportPayload.display_sample_count || 0).toLocaleString("zh-CN")} 点；CPU、GPU、温度、频率、功率及断线边界分别保峰。时间加权连续量、全程帧统计和峰值来自完整数据，不用抽样点重算。`;
      }
    }
    if (!rawMetrics.length) {
      const empty = document.createElement("div");
      empty.className = "raw-empty";
      empty.textContent = "本次没有形成可展示的有效原始时间序列。";
      container.appendChild(empty);
      return;
    }
    rawMetrics.forEach(metric => {
      const article = document.createElement("article");
      article.className = "raw-metric-card";
      article.setAttribute("data-raw-metric", metric.key);
      const heading = document.createElement("div");
      heading.className = "raw-metric-heading";
      const labelGroup = document.createElement("div");
      const label = document.createElement("strong");
      label.textContent = metric.label;
      const source = document.createElement("span");
      source.textContent = metric.source;
      labelGroup.append(label, source);
      const valueGroup = document.createElement("div");
      const current = document.createElement("strong");
      current.className = "raw-current-value";
      current.textContent = "n/a";
      const range = document.createElement("span");
      const statisticsKey = metric.statisticsKey || metric.key;
      const fullStatistics = reportMetricStatistics[statisticsKey];
      const pointValues = metric.points.map(point => point.value).filter(finite).map(Number);
      const pointMaximum = pointValues.length ? Math.max(...pointValues) : null;
      const summaryMaximum = finite(metric.summaryMaximum)
        ? Number(metric.summaryMaximum)
        : pointMaximum;
      if (finite(metric.summaryValue)) {
        const maximumLabel = metric.summaryMaximumLabel || (metric.sampleSeries ? "峰" : "窗口峰");
        range.textContent = `${metric.summaryLabel || "全程汇总"} ${format(metric.summaryValue, metric.unit)}${finite(summaryMaximum) ? ` · ${maximumLabel} ${format(summaryMaximum, metric.unit)}` : ""}`;
      } else if (metric.statisticKind === "quantile" || metric.statisticKind === "ratio") {
        range.textContent = `${metric.points.length.toLocaleString("zh-CN")} 个窗口${finite(pointMaximum) ? ` · 窗口峰 ${format(pointMaximum, metric.unit)}` : ""} · 不对窗口统计求算术均值`;
      } else if (fullStatistics && finite(fullStatistics.average) && finite(fullStatistics.maximum)) {
        range.textContent = `全量样本算术均值 ${format(fullStatistics.average, metric.unit)} · 峰 ${format(fullStatistics.maximum, metric.unit)}`;
      } else if (!reportPayload.downsampled && !metric.categories) {
        const average = pointValues.reduce((sum, value) => sum + value, 0) / pointValues.length;
        range.textContent = `显示点样本算术均值 ${format(average, metric.unit)} · 峰 ${format(pointMaximum, metric.unit)}`;
      } else {
        range.textContent = `${metric.points.length.toLocaleString("zh-CN")} 个显示点 · 不由抽样点重算统计`;
      }
      valueGroup.append(current, range);
      heading.append(labelGroup, valueGroup);
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("role", "img");
      metric.svg = svg;
      metric.valueNode = current;
      article.append(heading, svg);
      container.appendChild(article);
    });
    window.requestAnimationFrame(renderRawMetricCharts);
  }
  function nearestIndex(time) {
    let best = 0;
    let distance = Infinity;
    samples.forEach((sample, index) => {
      const next = Math.abs(Number(sample.elapsed_s) - time);
      if (next < distance) { best = index; distance = next; }
    });
    return best;
  }
  function domain(values) {
    const valid = values.filter(finite).map(Number);
    if (!valid.length) return [0, 1];
    const maximum = Math.max(0, ...valid);
    return [0, Math.max(1, maximum * 1.08)];
  }
  function quantile(values, ratio) {
    const ordered = values.filter(finite).map(Number).sort((a, b) => a - b);
    if (!ordered.length) return null;
    const position = Math.max(0, Math.min(1, Number(ratio))) * (ordered.length - 1);
    const lower = Math.floor(position), upper = Math.min(ordered.length - 1, lower + 1);
    const fraction = position - lower;
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction;
  }
  function frameIntervals() {
    return frameTimeline
      .flatMap(item => Array.isArray(item.frame_intervals_ms) ? item.frame_intervals_ms : [])
      .filter(value => finite(value) && Number(value) > 0 && Number(value) < 10000)
      .map(Number);
  }
  function pointStrings(values, x, y, rows = samples) {
    const segments = [];
    let current = [];
    values.forEach((value, index) => {
      if (!finite(value)) {
        if (current.length) segments.push(current);
        current = [];
        return;
      }
      current.push(`${x(rows[index].elapsed_s).toFixed(2)},${y(Number(value)).toFixed(2)}`);
    });
    if (current.length) segments.push(current);
    return segments.map(segment => segment.join(" "));
  }
  function reportTimelineMaximum() {
    let maximum = maxTime();
    rawMetrics.forEach(metric => {
      (metric.points || []).forEach(point => { maximum = Math.max(maximum, Number(point.elapsed) || 0); });
      (metric.overlays || []).forEach(overlay => {
        (overlay.points || []).forEach(point => { maximum = Math.max(maximum, Number(point.elapsed) || 0); });
      });
    });
    (Array.isArray(frameFlow.stages) ? frameFlow.stages : []).forEach(stage => {
      (Array.isArray(stage?.timeline) ? stage.timeline : []).forEach(point => {
        const elapsed = elapsedForPoint(point);
        if (elapsed != null) maximum = Math.max(maximum, elapsed);
      });
    });
    return Math.max(1, maximum);
  }
  function setExactReportRange(startElapsed, endElapsed, sourceKey = "", { requestFullSummary = false } = {}) {
    const maximum = reportTimelineMaximum();
    const start = Math.max(0, Math.min(maximum, Number(startElapsed) || 0));
    const end = Math.max(0, Math.min(maximum, Number(endElapsed) || 0));
    if (Math.abs(end - start) < 1e-6) return;
    reportRangeTouched = true;
    reportRangeStartElapsed = Math.min(start, end);
    reportRangeEndElapsed = Math.max(start, end);
    reportRangeSourceKey = String(sourceKey || reportRangeSourceKey || "");
    reportRangeFullSummary = null;
    reportRangeSummaryError = "";
    reportRangeSummaryRequestId += 1;
    if (reportRangeSummaryTimer) window.clearTimeout(reportRangeSummaryTimer);
    updateReportRangeEditor();
    refreshReportRangeSelectionOverlays();
    if (requestFullSummary) scheduleReportRangeFullSummary();
  }
  function drawReportRangeSelection(svg) {
    if (!svg || !svg.__reportRangeGeometry) return;
    svg.querySelectorAll(".report-range-window, .report-range-edge").forEach(node => node.remove());
    const selection = selectedReportRange();
    if (!selection) return;
    const geometry = svg.__reportRangeGeometry;
    const visibleStart = Math.max(geometry.rangeStart, selection.startElapsed);
    const visibleEnd = Math.min(geometry.rangeEnd, selection.endElapsed);
    if (visibleEnd <= visibleStart) return;
    const startX = geometry.x(visibleStart);
    const endX = geometry.x(visibleEnd);
    svg.appendChild(svgNode("rect", {
      x: startX,
      y: geometry.top,
      width: Math.max(1, endX - startX),
      height: Math.max(1, geometry.bottom - geometry.top),
      class: "raw-delete-window report-range-window",
    }));
    svg.appendChild(svgNode("line", {
      x1: startX, x2: startX, y1: geometry.top, y2: geometry.bottom, class: "report-range-edge",
    }));
    svg.appendChild(svgNode("line", {
      x1: endX, x2: endX, y1: geometry.top, y2: geometry.bottom, class: "report-range-edge",
    }));
  }
  function registerReportRangeSurface(svg, geometry) {
    if (!svg) return;
    svg.__reportRangeGeometry = geometry;
    drawReportRangeSelection(svg);
  }
  function refreshReportRangeSelectionOverlays() {
    root.querySelectorAll("svg").forEach(svg => drawReportRangeSelection(svg));
  }
  function attachTimeRangeBrush(svg, {
    width,
    height,
    left,
    right,
    top,
    bottom,
    rangeStart = 0,
    rangeEnd = maxTime(),
    sourceKey = "",
    hoverSelect = true,
  }) {
    const plotWidth = Math.max(1, width - left - right);
    const plotBottom = height - bottom;
    const x = time => left + (Math.max(rangeStart, Math.min(rangeEnd, Number(time))) - rangeStart) / Math.max(1e-9, rangeEnd - rangeStart) * plotWidth;
    registerReportRangeSurface(svg, { x, top, bottom: plotBottom, rangeStart, rangeEnd });
    const overlay = svgNode("rect", {
      x: left,
      y: top,
      width: plotWidth,
      height: Math.max(1, plotBottom - top),
      fill: "transparent",
      class: "time-range-brush",
      tabindex: "0",
      "aria-label": "横向拖动框选时间范围",
    });
    let drag = null;
    const timeForEvent = event => {
      const rect = svg.getBoundingClientRect();
      const localX = rect.width > 0 ? (event.clientX - rect.left) / rect.width * width : left;
      return Math.max(rangeStart, Math.min(
        rangeEnd,
        rangeStart + (localX - left) / plotWidth * (rangeEnd - rangeStart),
      ));
    };
    const finishDrag = event => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      const time = timeForEvent(event);
      const active = drag.active;
      if (active) setExactReportRange(drag.startTime, time, sourceKey, { requestFullSummary: true });
      drag = null;
      try { overlay.releasePointerCapture(event.pointerId); } catch (_error) { /* capture may already be released */ }
      selectSample(nearestIndex(time));
    };
    overlay.addEventListener("pointerdown", event => {
      if (!event.isPrimary || (event.button != null && event.button !== 0)) return;
      event.preventDefault();
      drag = {
        pointerId: event.pointerId,
        startClientX: event.clientX,
        startTime: timeForEvent(event),
        active: false,
        previousSelection: selectedReportRange(),
        previousFullSummary: reportRangeFullSummary,
        previousSummaryError: reportRangeSummaryError,
      };
      overlay.setPointerCapture(event.pointerId);
    });
    overlay.addEventListener("pointermove", event => {
      const time = timeForEvent(event);
      if (drag && event.pointerId === drag.pointerId) {
        event.preventDefault();
        if (!drag.active && Math.abs(event.clientX - drag.startClientX) >= 3) drag.active = true;
        if (drag.active) setExactReportRange(drag.startTime, time, sourceKey);
        return;
      }
      if (hoverSelect && event.pointerType === "mouse" && event.buttons === 0) {
        const index = nearestIndex(time);
        if (index !== selectedIndex) selectSample(index);
      }
    });
    overlay.addEventListener("pointerup", finishDrag);
    overlay.addEventListener("pointercancel", event => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      const previous = drag.previousSelection;
      const previousFullSummary = drag.previousFullSummary;
      const previousSummaryError = drag.previousSummaryError;
      const wasActive = drag.active;
      drag = null;
      try { overlay.releasePointerCapture(event.pointerId); } catch (_error) { /* capture may already be released */ }
      if (!wasActive) return;
      reportRangeSummaryRequestId += 1;
      if (reportRangeSummaryTimer) window.clearTimeout(reportRangeSummaryTimer);
      reportRangeTouched = Boolean(previous);
      if (previous) {
        reportRangeStartElapsed = previous.startElapsed;
        reportRangeEndElapsed = previous.endElapsed;
        reportRangeSourceKey = previous.sourceKey || "";
      }
      reportRangeFullSummary = previousFullSummary;
      reportRangeSummaryError = previousSummaryError;
      updateReportRangeEditor();
      refreshReportRangeSelectionOverlays();
    });
    svg.appendChild(overlay);
  }
  function attachOverlay(svg, width, height, left, right, top, bottom, rangeStart = 0, rangeEnd = maxTime()) {
    attachTimeRangeBrush(svg, {
      width,
      height,
      left,
      right,
      top,
      bottom,
      rangeStart,
      rangeEnd,
      sourceKey: svg.id || "timeline",
    });
  }

  function renderLanes(svg, lanes) {
    if (!svg || !samples.length || !lanes.length) return;
    const width = chartWidth(svg), compact = width < 620;
    const left = compact ? 106 : 150, right = compact ? 52 : 78, top = 18, bottom = 36, laneHeight = 94;
    const height = top + bottom + laneHeight * lanes.length;
    const plotWidth = width - left - right;
    const x = time => left + Number(time) / maxTime() * plotWidth;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.style.minHeight = `${Math.max(260, Math.min(720, height))}px`;
    svg.replaceChildren();
    for (let tick = 0; tick <= 5; tick++) {
      const seconds = maxTime() * tick / 5;
      const xPos = x(seconds);
      svg.appendChild(svgNode("line", { x1: xPos, x2: xPos, y1: top, y2: height - bottom, class: "grid" }));
      svg.appendChild(svgNode("text", { x: xPos, y: height - 12, "text-anchor": "middle", class: "axis-text" }, formatTime(seconds)));
    }
    lanes.forEach((lane, laneIndex) => {
      const laneTop = top + laneIndex * laneHeight;
      const laneBottom = laneTop + laneHeight - 18;
      const values = samples.map((sample, index) => lane.value(sample, index));
      const [minimum, maximum] = domain(values);
      const y = value => laneTop + 10 + (maximum - value) / (maximum - minimum) * (laneBottom - laneTop - 15);
      svg.appendChild(svgNode("text", { x: 12, y: laneTop + 24, class: "lane-label" }, lane.label));
      svg.appendChild(svgNode("text", { x: 12, y: laneTop + 44, class: "lane-value" }, format(values[selectedIndex], lane.unit)));
      svg.appendChild(svgNode("text", { x: width - 8, y: laneTop + 17, "text-anchor": "end", class: "axis-text" }, format(maximum, lane.unit)));
      svg.appendChild(svgNode("text", { x: width - 8, y: laneBottom, "text-anchor": "end", class: "axis-text" }, format(minimum, lane.unit)));
      svg.appendChild(svgNode("line", { x1: left, x2: width - right, y1: laneBottom + 8, y2: laneBottom + 8, class: "grid" }));
      pointStrings(values, x, y).forEach(points => {
        svg.appendChild(svgNode("polyline", { points, fill: "none", stroke: lane.color, "stroke-width": 1.8 }));
      });
      const selectedValue = values[selectedIndex];
      if (finite(selectedValue)) svg.appendChild(svgNode("circle", { cx: x(samples[selectedIndex].elapsed_s), cy: y(Number(selectedValue)), r: 3.5, class: "selected-point", stroke: lane.color }));
    });
    appendBrightnessMarkers(svg, x, top, height - bottom);
    const selectedX = x(samples[selectedIndex].elapsed_s);
    svg.appendChild(svgNode("line", { x1: selectedX, x2: selectedX, y1: top, y2: height - bottom, class: "crosshair" }));
    attachOverlay(svg, width, height, left, right, top, bottom);
  }

  function renderFrameFlowHistory() {
    const svg = root.querySelector("#frame-flow-history-chart");
    const stages = Array.isArray(frameFlow.stages) ? frameFlow.stages : [];
    if (!svg || !stages.length) return;
    const laneColors = {
      app_submission: colors[0],
      render_queue: colors[1],
      surface_present: colors[4],
      display_scanout: "#9e8cff",
    };
    const startUptime = sessionStartUptime();
    const lanes = stages.map((stage, index) => {
      const points = (Array.isArray(stage.timeline) ? stage.timeline : [])
        .map(point => {
          const elapsed = finite(point.elapsed_s)
            ? Number(point.elapsed_s)
            : finite(point.uptime_s) ? Math.max(0, Number(point.uptime_s) - startUptime) : null;
          const value = finite(point.value)
            ? Number(point.value)
            : finite(point.frame_rate_fps)
              ? Number(point.frame_rate_fps)
              : finite(point.refresh_rate_hz) ? Number(point.refresh_rate_hz) : null;
          return elapsed == null || value == null ? null : { elapsed, value };
        })
        .filter(Boolean)
        .sort((left, right) => left.elapsed - right.elapsed);
      return {
        key: String(stage.key || `stage-${index}`),
        phase: String(stage.phase || "STAGE"),
        label: String(stage.label || stage.key || `节点 ${index + 1}`),
        valueLabel: String(stage.timeline_value_label || "帧率"),
        unit: String(stage.timeline_unit || "FPS"),
        status: String(stage.status || "unavailable"),
        color: laneColors[stage.key] || colors[index % colors.length],
        points,
      };
    });
    const allPoints = lanes.flatMap(lane => lane.points);
    const width = chartWidth(svg), compact = width < 680;
    const left = compact ? 120 : 166, right = compact ? 48 : 66, top = 22, bottom = 36, laneHeight = compact ? 76 : 82;
    const height = top + bottom + laneHeight * lanes.length;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.style.minHeight = `${height}px`;
    svg.replaceChildren();
    const maximumTime = Math.max(maxTime(), ...allPoints.map(point => point.elapsed));
    const observedMaximum = Math.max(
      1,
      Number(performance.peak_refresh_rate_hz || 0),
      ...allPoints.map(point => point.value),
    );
    const maximumValue = Math.max(30, Math.ceil(observedMaximum / 30) * 30);
    const x = value => left + Math.max(0, Math.min(maximumTime, Number(value))) / maximumTime * (width - left - right);
    const selectedElapsed = Number(samples[selectedIndex]?.elapsed_s || 0);
    for (let tick = 0; tick <= 5; tick++) {
      const elapsed = maximumTime * tick / 5;
      const xPos = x(elapsed);
      svg.appendChild(svgNode("line", { x1: xPos, x2: xPos, y1: top, y2: height - bottom, class: "grid" }));
      svg.appendChild(svgNode("text", { x: xPos, y: height - 12, "text-anchor": tick === 0 ? "start" : tick === 5 ? "end" : "middle", class: "axis-text" }, formatTime(elapsed)));
    }
    lanes.forEach((lane, laneIndex) => {
      const laneTop = top + laneIndex * laneHeight;
      const laneBottom = laneTop + laneHeight - 15;
      const y = value => laneBottom - Math.max(0, Math.min(maximumValue, Number(value))) / maximumValue * (laneBottom - laneTop - 12);
      const selectedPoint = lane.points.reduce(
        (match, point) => point.elapsed <= selectedElapsed ? point : match,
        null,
      );
      svg.appendChild(svgNode("text", { x: 12, y: laneTop + 24, class: "flow-lane-label" }, `${lane.phase} · ${lane.label}`));
      svg.appendChild(svgNode("text", { x: 12, y: laneTop + 43, class: "flow-lane-value" }, selectedPoint ? `${lane.valueLabel} ${format(selectedPoint.value, lane.unit)}` : lane.valueLabel));
      svg.appendChild(svgNode("text", { x: width - 8, y: laneTop + 14, "text-anchor": "end", class: "axis-text" }, format(maximumValue, lane.unit)));
      svg.appendChild(svgNode("text", { x: width - 8, y: laneBottom, "text-anchor": "end", class: "axis-text" }, `0 ${lane.unit}`));
      svg.appendChild(svgNode("line", { x1: left, x2: width - right, y1: laneBottom + 5, y2: laneBottom + 5, class: "grid" }));
      if (!lane.points.length) {
        const emptyLabel = lane.key === "display_scanout"
          ? "暂无有效刷新率时间序列"
          : lane.key === "render_queue"
            ? ["primary", "valid", "reference"].includes(lane.status)
              ? "该节点仅提供阶段耗时，没有独立 FPS 计数"
              : "未获得可解析的 RenderThread / BufferQueue 阶段数据"
            : "平台未公开该节点的独立帧率时间序列";
        svg.appendChild(svgNode("text", { x: left + 10, y: laneTop + 35, class: "flow-lane-empty" }, emptyLabel));
        return;
      }
      const coordinates = lane.points.map(point => ({ ...point, x: x(point.elapsed), y: y(point.value) }));
      let path = `M${coordinates[0].x.toFixed(2)},${coordinates[0].y.toFixed(2)}`;
      for (let index = 1; index < coordinates.length; index += 1) {
        const previous = coordinates[index - 1], current = coordinates[index];
        path += lane.key === "display_scanout"
          ? ` L${current.x.toFixed(2)},${previous.y.toFixed(2)} L${current.x.toFixed(2)},${current.y.toFixed(2)}`
          : ` L${current.x.toFixed(2)},${current.y.toFixed(2)}`;
      }
      if (lane.key === "display_scanout" && coordinates.at(-1).elapsed < maximumTime) {
        path += ` L${x(maximumTime).toFixed(2)},${coordinates.at(-1).y.toFixed(2)}`;
      }
      svg.appendChild(svgNode("path", { d: path, class: `flow-lane-line ${lane.status}`, stroke: lane.color }));
      const last = coordinates.at(-1);
      svg.appendChild(svgNode("circle", { cx: last.x, cy: last.y, r: 3.4, class: "flow-lane-dot", fill: lane.color }));
      if (selectedPoint) {
        svg.appendChild(svgNode("circle", { cx: x(selectedPoint.elapsed), cy: y(selectedPoint.value), r: 4.1, class: "selected-point", stroke: lane.color }));
      }
    });
    const selectedX = x(selectedElapsed);
    svg.appendChild(svgNode("line", { x1: selectedX, x2: selectedX, y1: top, y2: height - bottom, class: "crosshair" }));
    attachTimeRangeBrush(svg, {
      width,
      height,
      left,
      right,
      top,
      bottom,
      rangeStart: 0,
      rangeEnd: maximumTime,
      sourceKey: "frame_flow",
      hoverSelect: false,
    });
  }

  function timelineLanes() {
    const reportPower = reportPowerChannelPresentation();
    const lanes = testMode === "performance"
      ? [
          { label: "帧率", unit: "FPS", color: colors[0], value: sample => (frameForUptime(sample.uptime_s) || {}).frame_rate_fps },
          { label: "P99 帧耗时", unit: "ms", color: colors[3], value: sample => (frameForUptime(sample.uptime_s) || {}).frame_time_p99_ms || (frameForUptime(sample.uptime_s) || {}).frame_time_p95_ms },
          { label: "CPU 总负载", unit: "%", color: colors[2], value: sample => sample.cpu_pct }
        ]
      : [
          { label: reportPower.label, unit: "mW", color: colors[0], value: sample => reportPrimaryPowerValue(sample, reportPower) },
          { label: "电流", unit: "mA", color: colors[1], value: sample => sample.current_ma },
          { label: "CPU 总负载", unit: "%", color: colors[2], value: sample => sample.cpu_pct }
        ];
    cpu.clusters.forEach((cluster, index) => lanes.push({ label: `${clusterLabel(cluster)}频率`, unit: "MHz", color: colors[(index + 3) % colors.length], value: sample => (sample.frequencies_mhz || {})[cluster.name] }));
    if (gpu.frequency_available) lanes.push({ label: "GPU 频率", unit: "MHz", color: colors[5], value: sample => sample.gpu_frequency_mhz });
    if (gpu.load_available) lanes.push({ label: "GPU 负载", unit: "%", color: colors[1], value: sample => sample.gpu_load_pct });
    if (samples.some(sample => finite(sample.memory_frequency_mhz))) lanes.push({ label: "内存频率", unit: "MHz", color: colors[4], value: sample => sample.memory_frequency_mhz });
    if (testMode === "performance") lanes.push({ label: reportPower.label, unit: "mW", color: colors[5], value: sample => reportPrimaryPowerValue(sample, reportPower) });
    return lanes;
  }
  function renderTimeline() { renderLanes(root.querySelector("#timeline-chart"), timelineLanes()); }

  function renderFrameStability() {
    const svg = root.querySelector("#frame-interval-chart");
    const intervals = frameIntervals();
    if (!svg || !intervals.length) return;
    const width = chartWidth(svg), height = 310;
    const margin = { left: width < 560 ? 48 : 58, right: 24, top: 30, bottom: 48 };
    const budget = finite(svg.dataset.budgetMs) && Number(svg.dataset.budgetMs) > 0
      ? Number(svg.dataset.budgetMs)
      : null;
    const issueThreshold = finite(svg.dataset.issueMs) && Number(svg.dataset.issueMs) > 0
      ? Number(svg.dataset.issueMs)
      : budget != null ? budget * 1.5 : null;
    let dynamicBudgetLines = [];
    try {
      const parsed = JSON.parse(svg.dataset.budgetLines || "[]");
      dynamicBudgetLines = Array.isArray(parsed)
        ? parsed.filter(item => finite(item?.budget_ms) && Number(item.budget_ms) > 0)
        : [];
    } catch (_error) {
      dynamicBudgetLines = [];
    }
    const budgetValues = dynamicBudgetLines.map(item => Number(item.budget_ms));
    const q01 = quantile(intervals, .01) || Math.min(...intervals);
    const q99 = quantile(intervals, .99) || Math.max(...intervals);
    const smallestBudget = budgetValues.length ? Math.min(...budgetValues) : null;
    const largestBudget = budgetValues.length ? Math.max(...budgetValues) : null;
    let minimum = Math.max(0, budget ? Math.min(q01, budget * .72) : smallestBudget ? Math.min(q01, smallestBudget * .72) : q01 * .82);
    let maximum = budget
      ? Math.max(budget * 1.6, q99 * 1.08)
      : largestBudget ? Math.max(largestBudget * 1.18, q99 * 1.08) : q99 * 1.14;
    if (!finite(maximum) || maximum <= minimum) maximum = minimum + Math.max(1, minimum * .25);
    const binCount = width < 560 ? 14 : 22;
    const binWidth = (maximum - minimum) / binCount;
    const counts = Array.from({ length: binCount }, () => 0);
    let lowerTail = 0, upperTail = 0;
    intervals.forEach(value => {
      if (value < minimum) lowerTail += 1;
      if (value > maximum) upperTail += 1;
      const index = Math.max(0, Math.min(binCount - 1, Math.floor((value - minimum) / binWidth)));
      counts[index] += 1;
    });
    const peak = Math.max(1, ...counts);
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const x = value => margin.left + (Number(value) - minimum) / (maximum - minimum) * plotWidth;
    const y = value => margin.top + (peak - Number(value)) / peak * plotHeight;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.replaceChildren();
    for (let tick = 0; tick <= 4; tick++) {
      const count = peak * (4 - tick) / 4;
      const yPos = margin.top + plotHeight * tick / 4;
      svg.appendChild(svgNode("line", { x1: margin.left, x2: width - margin.right, y1: yPos, y2: yPos, class: "grid" }));
      svg.appendChild(svgNode("text", { x: margin.left - 8, y: yPos + 4, "text-anchor": "end", class: "axis-text" }, String(Math.round(count))));
    }
    for (let tick = 0; tick <= 4; tick++) {
      const value = minimum + (maximum - minimum) * tick / 4;
      const xPos = x(value);
      svg.appendChild(svgNode("line", { x1: xPos, x2: xPos, y1: margin.top, y2: height - margin.bottom, class: "grid" }));
      svg.appendChild(svgNode("text", { x: xPos, y: height - 18, "text-anchor": "middle", class: "axis-text" }, `${value.toFixed(value >= 10 ? 1 : 2)} ms`));
    }
    const slotWidth = plotWidth / binCount;
    counts.forEach((count, index) => {
      const start = minimum + index * binWidth;
      const end = start + binWidth;
      const tail = (issueThreshold != null && end > issueThreshold) || (index === binCount - 1 && upperTail > 0);
      const edge = !tail && budget != null && end > budget;
      const barHeight = Math.max(count ? 1 : 0, plotHeight - (y(count) - margin.top));
      svg.appendChild(svgNode("rect", {
        x: margin.left + index * slotWidth + 1,
        y: height - margin.bottom - barHeight,
        width: Math.max(1, slotWidth - 2),
        height: barHeight,
        class: `histogram-bar${tail ? " tail" : edge ? " edge" : ""}`
      }));
    });
    if (budget != null && budget >= minimum && budget <= maximum) {
      const budgetX = x(budget);
      svg.appendChild(svgNode("line", { x1: budgetX, x2: budgetX, y1: margin.top, y2: height - margin.bottom, class: "budget-line" }));
      svg.appendChild(svgNode("text", { x: Math.min(width - margin.right, budgetX + 6), y: margin.top + 13, class: "budget-label" }, `帧预算 ${budget.toFixed(2)} ms`));
    } else if (dynamicBudgetLines.length > 1) {
      dynamicBudgetLines.forEach((item, index) => {
        const itemBudget = Number(item.budget_ms);
        if (itemBudget < minimum || itemBudget > maximum) return;
        const budgetX = x(itemBudget);
        svg.appendChild(svgNode("line", { x1: budgetX, x2: budgetX, y1: margin.top, y2: height - margin.bottom, class: "budget-line" }));
        svg.appendChild(svgNode("text", {
          x: Math.min(width - margin.right, budgetX + 5),
          y: margin.top + 12 + index * 13,
          class: "budget-label",
        }, `${Number(item.refresh_hz).toFixed(0)} Hz · ${itemBudget.toFixed(2)} ms`));
      });
    }
    svg.appendChild(svgNode("text", { x: margin.left, y: 17, class: "axis-text" }, `样本 ${intervals.length.toLocaleString("zh-CN")} · 柱高为帧数`));
    const tailText = [lowerTail ? `<${minimum.toFixed(2)} ms: ${lowerTail}` : "", upperTail ? `>${maximum.toFixed(2)} ms: ${upperTail}` : ""].filter(Boolean).join(" · ");
    if (tailText) svg.appendChild(svgNode("text", { x: width - margin.right, y: 17, "text-anchor": "end", class: "axis-text" }, `长尾汇总 ${tailText}`));
  }

  function renderFlow() {
    if (testMode !== "power") return;
    const svg = root.querySelector("#flow-chart");
    if (!svg || !samples.length) return;
    const reportPower = reportPowerChannelPresentation();
    const width = chartWidth(svg), height = 320;
    const left = width < 620 ? 88 : 124, right = 20, top = 24, powerBottom = 202, bandTop = 232, bandHeight = 32, bottom = 38;
    const plotWidth = width - left - right;
    const x = time => left + Math.max(0, Math.min(maxTime(), Number(time))) / maxTime() * plotWidth;
    const powers = samples.map(sample => reportPrimaryPowerValue(sample, reportPower));
    const [minimum, maximum] = domain(powers);
    const y = value => top + (maximum - value) / (maximum - minimum) * (powerBottom - top);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.replaceChildren();

    for (let tick = 0; tick <= 5; tick++) {
      const seconds = maxTime() * tick / 5;
      const xPos = x(seconds);
      svg.appendChild(svgNode("line", { x1: xPos, x2: xPos, y1: top, y2: bandTop + bandHeight, class: "grid" }));
      svg.appendChild(svgNode("text", { x: xPos, y: height - 12, "text-anchor": "middle", class: "axis-text" }, formatTime(seconds)));
    }
    svg.appendChild(svgNode("text", { x: 12, y: top + 18, class: "lane-label" }, reportPower.label));
    svg.appendChild(svgNode("text", { x: 12, y: top + 38, class: "lane-value" }, format(powers[selectedIndex], "mW")));
    svg.appendChild(svgNode("text", { x: 12, y: bandTop + 20, class: "lane-label" }, "前台应用"));

    events.forEach(event => {
      const start = Number(event.device_uptime_s) - sessionStartUptime();
      const duration = Number(event.duration_s || 0);
      if (start > maxTime() || start + duration < 0) return;
      if (duration > 0) {
        svg.appendChild(svgNode("rect", {
          x: x(start), y: top, width: Math.max(1, x(start + duration) - x(start)), height: powerBottom - top, class: "event-span"
        }));
      } else {
        svg.appendChild(svgNode("line", { x1: x(start), x2: x(start), y1: top, y2: powerBottom, class: "event-line" }));
      }
    });

    pointStrings(powers, x, y).forEach(points => {
      svg.appendChild(svgNode("polyline", { points, fill: "none", stroke: colors[0], "stroke-width": 2 }));
    });

    const startUptime = sessionStartUptime();
    let cursor = 0;
    let currentContext = contextForUptime(startUptime);
    let currentPackage = currentContext && currentContext.foreground_package ? currentContext.foreground_package : "未知";
    const segments = [];
    contexts.forEach(context => {
      const elapsed = Number(context.uptime_s) - startUptime;
      if (elapsed <= 0 || elapsed > maxTime()) return;
      const nextPackage = context.foreground_package || "未知";
      if (nextPackage === currentPackage) { currentContext = context; return; }
      segments.push({ start: cursor, end: elapsed, package: currentPackage });
      cursor = elapsed;
      currentContext = context;
      currentPackage = nextPackage;
    });
    segments.push({ start: cursor, end: maxTime(), package: currentPackage });
    const appColors = new Map();
    segments.forEach(segment => {
      if (!appColors.has(segment.package)) appColors.set(segment.package, colors[appColors.size % colors.length]);
      const startX = x(segment.start), endX = x(segment.end);
      svg.appendChild(svgNode("rect", { x: startX, y: bandTop, width: Math.max(1, endX - startX), height: bandHeight, fill: appColors.get(segment.package), class: "app-band" }));
      if (endX - startX > 92) {
        const label = segment.package.length > 24 ? `...${segment.package.slice(-21)}` : segment.package;
        svg.appendChild(svgNode("text", { x: startX + 6, y: bandTop + 21, class: "band-label" }, label));
      }
    });

    let lastLabelX = -Infinity;
    events.slice(0, 200).forEach(event => {
      const elapsed = Number(event.device_uptime_s) - startUptime;
      if (elapsed < 0 || elapsed > maxTime()) return;
      const eventX = x(elapsed);
      if (eventX - lastLabelX > 90) {
        svg.appendChild(svgNode("text", { x: eventX + 4, y: top + 13, class: "axis-text" }, String(event.name || event.phase || "event").slice(0, 26)));
        lastLabelX = eventX;
      }
    });
    const selectedX = x(samples[selectedIndex].elapsed_s);
    svg.appendChild(svgNode("line", { x1: selectedX, x2: selectedX, y1: top, y2: bandTop + bandHeight, class: "crosshair" }));
    attachOverlay(svg, width, height, left, right, top, bottom);
  }

  function renderTestItems() {
    const svg = root.querySelector("#test-item-chart");
    if (!svg || !samples.length) return;
    const reportPower = reportPowerChannelPresentation();
    const width = chartWidth(svg), left = width < 660 ? 108 : 146, right = 24;
    const powerTop = 22, powerBottom = 180, laneStart = 208, laneHeight = 48, bottom = 38;
    const laneNames = ["前台应用", "测试项 / 阶段", "系统活动", "Thermal", "热 / 调度上下文"];
    const height = laneStart + laneHeight * laneNames.length + bottom;
    const rangeStart = testRange ? Math.max(0, Number(testRange[0])) : 0;
    const rangeEnd = testRange ? Math.min(maxTime(), Number(testRange[1])) : maxTime();
    const safeEnd = Math.max(rangeStart + 1, rangeEnd);
    const plotWidth = width - left - right;
    const x = time => left + (Math.max(rangeStart, Math.min(safeEnd, Number(time))) - rangeStart) / (safeEnd - rangeStart) * plotWidth;
    const visibleSamples = samples.filter(sample => Number(sample.elapsed_s) >= rangeStart && Number(sample.elapsed_s) <= safeEnd);
    const primaryValues = visibleSamples.map(sample => testMode === "performance" ? (frameForUptime(sample.uptime_s) || {}).frame_rate_fps : reportPrimaryPowerValue(sample, reportPower));
    const [minimum, maximum] = domain(primaryValues);
    const y = value => powerTop + (maximum - value) / (maximum - minimum) * (powerBottom - powerTop);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.style.minHeight = `${height}px`;
    svg.replaceChildren();

    for (let tick = 0; tick <= 6; tick++) {
      const seconds = rangeStart + (safeEnd - rangeStart) * tick / 6;
      const xPos = x(seconds);
      svg.appendChild(svgNode("line", { x1: xPos, x2: xPos, y1: powerTop, y2: laneStart + laneHeight * laneNames.length, class: "grid" }));
      svg.appendChild(svgNode("text", { x: xPos, y: height - 12, "text-anchor": "middle", class: "axis-text" }, formatTime(seconds)));
    }
    svg.appendChild(svgNode("text", { x: 12, y: powerTop + 20, class: "lane-label" }, testMode === "performance" ? "帧率" : reportPower.label));
    const selected = samples[selectedIndex];
    const selectedPrimary = testMode === "performance" ? (frameForUptime(selected && selected.uptime_s) || {}).frame_rate_fps : selected && reportPrimaryPowerValue(selected, reportPower);
    svg.appendChild(svgNode("text", { x: 12, y: powerTop + 41, class: "lane-value" }, format(selectedPrimary, testMode === "performance" ? "FPS" : "mW")));
    laneNames.forEach((name, index) => {
      const top = laneStart + index * laneHeight;
      svg.appendChild(svgNode("text", { x: 12, y: top + 25, class: "lane-label" }, name));
      svg.appendChild(svgNode("line", { x1: left, x2: width - right, y1: top + laneHeight - 2, y2: top + laneHeight - 2, class: "grid" }));
    });
    pointStrings(primaryValues, x, y, visibleSamples).forEach(points => {
      svg.appendChild(svgNode("polyline", { points, fill: "none", stroke: colors[0], "stroke-width": 2 }));
    });

    const startUptime = sessionStartUptime();
    const appTop = laneStart + 8;
    let cursor = rangeStart;
    let activeContext = contextForUptime(startUptime + rangeStart);
    let activePackage = activeContext && activeContext.foreground_package ? activeContext.foreground_package : "未知";
    const appSegments = [];
    contexts.forEach(context => {
      const elapsed = Number(context.uptime_s) - startUptime;
      if (elapsed <= rangeStart || elapsed > safeEnd) return;
      const nextPackage = context.foreground_package || "未知";
      if (nextPackage !== activePackage) {
        appSegments.push({ start: cursor, end: elapsed, label: activePackage });
        cursor = elapsed;
        activePackage = nextPackage;
      }
    });
    appSegments.push({ start: cursor, end: safeEnd, label: activePackage });
    const appColors = new Map();
    appSegments.forEach(segment => {
      if (!appColors.has(segment.label)) appColors.set(segment.label, colors[appColors.size % colors.length]);
      const startX = x(segment.start), endX = x(segment.end);
      svg.appendChild(svgNode("rect", { x: startX, y: appTop, width: Math.max(1, endX - startX), height: 28, fill: appColors.get(segment.label), class: "app-band" }));
      if (endX - startX > 90) svg.appendChild(svgNode("text", { x: startX + 5, y: appTop + 19, class: "band-label" }, String(segment.label).slice(-28)));
    });

    const testTop = laneStart + laneHeight + 7;
    (testItems.spans || []).forEach((span, index) => {
      const start = Number(span.start_elapsed_s || 0), end = Number(span.end_elapsed_s || start);
      if (end < rangeStart || start > safeEnd) return;
      const startX = x(Math.max(start, rangeStart)), endX = x(Math.min(end, safeEnd));
      const offset = (index % 2) * 15;
      svg.appendChild(svgNode("rect", { x: startX, y: testTop + offset, width: Math.max(1, endX - startX), height: 13, class: "test-band" }));
      if (endX - startX > 84) svg.appendChild(svgNode("text", { x: startX + 4, y: testTop + offset + 11, class: "band-label" }, String(span.name || span.phase || "测试项").slice(0, 22)));
    });
    (testItems.instant_events || []).forEach(event => {
      const elapsed = Number(event.elapsed_s || 0);
      if (elapsed < rangeStart || elapsed > safeEnd) return;
      svg.appendChild(svgNode("line", { x1: x(elapsed), x2: x(elapsed), y1: testTop, y2: testTop + 28, class: "event-line" }));
    });

    const familyColors = { gc: colors[3], kworker: colors[2], rcu: colors[4], irq: colors[3], display: colors[5], dex_optimization: colors[1], system_update: colors[3], package_management: colors[4] };
    const activityTop = laneStart + laneHeight * 2 + 7;
    const groupedRows = ((((analysis.system || {}).activity_groups || {}).rows) || []);
    const priorityRows = ((((analysis.system || {}).priority_activities || {}).rows) || []);
    const systemBands = [];
    [...groupedRows, ...priorityRows].forEach(row => {
      (row.windows || []).forEach(window => {
        const start = finite(window.start_elapsed_s) ? Number(window.start_elapsed_s) : Number(window.start_uptime_s || startUptime) - startUptime;
        const end = finite(window.end_elapsed_s) ? Number(window.end_elapsed_s) : Number(window.end_uptime_s || startUptime) - startUptime;
        systemBands.push({ start, end, label: row.label || row.name || row.kind, family: row.family || row.kind || "system" });
      });
    });
    systemBands.sort((a, b) => a.start - b.start);
    systemBands.forEach((band, index) => {
      if (band.end < rangeStart || band.start > safeEnd) return;
      const startX = x(Math.max(rangeStart, band.start)), endX = x(Math.min(safeEnd, band.end));
      const offset = (index % 2) * 15;
      svg.appendChild(svgNode("rect", { x: startX, y: activityTop + offset, width: Math.max(1, endX - startX), height: 13, fill: familyColors[band.family] || colors[4], class: "activity-band" }));
      if (endX - startX > 76) svg.appendChild(svgNode("text", { x: startX + 4, y: activityTop + offset + 11, class: "band-label" }, String(band.label).slice(0, 20)));
    });

    const thermalTop = laneStart + laneHeight * 3 + 9;
    const thermalTimeline = ((analysis.thermal || {}).timeline || []).slice().sort((a, b) => Number(a.elapsed_s) - Number(b.elapsed_s));
    thermalTimeline.forEach((point, index) => {
      const start = Number(point.elapsed_s || 0);
      const end = index + 1 < thermalTimeline.length ? Number(thermalTimeline[index + 1].elapsed_s || start) : maxTime();
      if (end < rangeStart || start > safeEnd) return;
      const status = Number(point.status || 0);
      const fill = status > 0 ? colors[3] : colors[1];
      const startX = x(Math.max(rangeStart, start)), endX = x(Math.min(safeEnd, end));
      svg.appendChild(svgNode("rect", { x: startX, y: thermalTop, width: Math.max(1, endX - startX), height: 24, fill, opacity: status > 0 ? Math.min(.8, .22 + status * .1) : .18 }));
      if (status > 0 && endX - startX > 48) svg.appendChild(svgNode("text", { x: startX + 4, y: thermalTop + 17, class: "band-label" }, `状态 ${status}`));
    });

    const schedulerTop = laneStart + laneHeight * 4 + 9;
    const schedulerTimeline = ((analysis.scheduler || {}).timeline || []).slice().sort((a, b) => Number(a.elapsed_s) - Number(b.elapsed_s));
    schedulerTimeline.forEach((point, index) => {
      const start = Number(point.elapsed_s || 0);
      const end = index + 1 < schedulerTimeline.length ? Number(schedulerTimeline[index + 1].elapsed_s || start) : maxTime();
      if (end < rangeStart || start > safeEnd) return;
      const count = Number(point.hint_session_count || 0);
      if (count <= 0) return;
      const startX = x(Math.max(rangeStart, start)), endX = x(Math.min(safeEnd, end));
      svg.appendChild(svgNode("rect", { x: startX, y: schedulerTop, width: Math.max(1, endX - startX), height: 24, class: "scheduler-band" }));
      if (endX - startX > 54) svg.appendChild(svgNode("text", { x: startX + 4, y: schedulerTop + 17, class: "band-label" }, `ADPF ${count}`));
    });

    if (selected && Number(selected.elapsed_s) >= rangeStart && Number(selected.elapsed_s) <= safeEnd) {
      const selectedX = x(selected.elapsed_s);
      svg.appendChild(svgNode("line", { x1: selectedX, x2: selectedX, y1: powerTop, y2: laneStart + laneHeight * laneNames.length, class: "crosshair" }));
    }
    attachOverlay(svg, width, height, left, right, powerTop, bottom, rangeStart, safeEnd);
  }

  function renderCpu() {
    const svg = root.querySelector("#cpu-chart");
    const cluster = cpu.clusters.find(item => item.name === selectedCluster);
    if (!svg || !cluster) return;
    const lanes = [
      { label: `${clusterLabel(cluster)}频率`, unit: "MHz", color: colors[0], value: sample => (sample.frequencies_mhz || {})[cluster.name] }
    ];
    renderLanes(svg, lanes);
  }

  function renderGpu() {
    const surface = root.querySelector("#gpu-chart-surface");
    const svg = root.querySelector("#gpu-chart");
    if (!surface || !svg) return;
    if (!gpu.frequency_available && !gpu.load_available) { surface.hidden = true; return; }
    const lanes = [];
    if (gpu.frequency_available) lanes.push({ label: "GPU 频率", unit: "MHz", color: colors[5], value: sample => sample.gpu_frequency_mhz });
    if (gpu.load_available) lanes.push({ label: "GPU 负载", unit: "%", color: colors[1], value: sample => sample.gpu_load_pct });
    renderLanes(svg, lanes);
  }

  function updateSampleDetail() {
    const sample = samples[selectedIndex];
    const detail = root.querySelector("#sample-detail");
    const slider = root.querySelector("#overview-slider");
    if (!sample || !detail) return;
    if (slider) slider.value = String(selectedIndex);
    const context = contextForUptime(sample.uptime_s);
    const packageName = context && context.foreground_package ? context.foreground_package : "未知";
    const powerValidity = sample.power_valid_for_consumption === true
      ? "有效放电"
      : sample.external_power === true
        ? "外部供电 · 仅原始流量"
        : "不进入耗电结论";
    const directionLabel = sample.external_power === true || sample.direction === "external_power"
      ? "外部供电"
      : sample.direction === "discharging"
        ? "电池放电"
        : sample.direction === "charging"
          ? "电池充入"
          : sample.direction === "idle" || sample.direction === "full"
            ? "电池净流量接近零"
            : "未知";
    const signedCurrent = finite(sample.signed_current_ma)
      ? `${Number(sample.signed_current_ma) >= 0 ? "+" : ""}${Number(sample.signed_current_ma).toFixed(1)} mA`
      : "未知";
    detail.innerHTML = `<span>同步时间 <strong>${formatTime(sample.elapsed_s)}</strong></span>`
      + `<span>样本 <strong>${selectedIndex + 1} / ${samples.length}</strong></span>`
      + `<span>有效曲线 <strong>${rawMetrics.length}</strong></span>`
      + `<span>功率通道 <strong>${sample.power_source || "电池基础通道"}</strong></span>`
      + `<span>样本语义 <strong>${powerValidity}</strong></span>`
      + `<span>电池方向 <strong>${directionLabel} · ${signedCurrent}</strong></span>`
      + `<span>前台 <strong>${packageName}</strong></span>`;
  }
  function selectSample(index) {
    selectedIndex = Math.max(0, Math.min(samples.length - 1, Number(index)));
    updateSampleDetail();
    renderRawMetricCharts();
    renderFrameFlowHistory();
  }
  function scheduleReportRangeFullSummary() {
    if (reportRangeSummaryTimer) window.clearTimeout(reportRangeSummaryTimer);
    const selection = selectedReportRange();
    if (!selection || !reportRunName) {
      updateReportRangeSummary();
      return;
    }
    const requestId = ++reportRangeSummaryRequestId;
    reportRangeSummaryError = "";
    reportRangeSummaryTimer = window.setTimeout(async () => {
      reportRangeSummaryTimer = null;
      try {
        const response = await fetch("/api/range-summary", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            run_name: reportRunName,
            start_elapsed_s: selection.startElapsed,
            end_elapsed_s: selection.endElapsed,
          }),
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
        if (requestId !== reportRangeSummaryRequestId) return;
        const current = selectedReportRange();
        if (
          !current
          || Math.abs(current.startElapsed - selection.startElapsed) > 1e-6
          || Math.abs(current.endElapsed - selection.endElapsed) > 1e-6
        ) return;
        reportRangeFullSummary = result && result.full_resolution === true ? result : null;
        reportRangeSummaryError = reportRangeFullSummary ? "" : "服务端未返回全量统计";
      } catch (error) {
        if (requestId !== reportRangeSummaryRequestId) return;
        reportRangeFullSummary = null;
        reportRangeSummaryError = String(error?.message || error || "请求失败");
      }
      updateReportRangeSummary();
    }, 260);
  }
  function reportRangeSourceLabel(key) {
    if (key === "frame_flow") return "完整渲染链路";
    if (key === "range_sliders") return "滑块微调";
    const metric = rawMetrics.find(item => item.key === key);
    if (metric) return metric.label;
    const node = key ? root.ownerDocument.getElementById(key) : null;
    const ariaLabel = node?.getAttribute("aria-label");
    return ariaLabel || "时间图";
  }
  function reportRangeSeriesStatistics(points, selection) {
    const values = (Array.isArray(points) ? points : [])
      .filter(point => (
        finite(point?.elapsed)
        && Number(point.elapsed) >= selection.startElapsed
        && Number(point.elapsed) <= selection.endElapsed
        && !elapsedInsideReportExclusion(Number(point.elapsed))
        && finite(point?.value)
      ))
      .map(point => Number(point.value));
    if (!values.length) return { count: 0, average: null, minimum: null, maximum: null };
    return {
      count: values.length,
      average: values.reduce((sum, value) => sum + value, 0) / values.length,
      minimum: Math.min(...values),
      maximum: Math.max(...values),
    };
  }
  function reportRangeStatisticRows(selection) {
    const rows = [];
    const fullMetrics = reportRangeFullSummary?.metrics && typeof reportRangeFullSummary.metrics === "object"
      ? reportRangeFullSummary.metrics
      : {};
    const resolvedStatistics = (key, points) => {
      const preview = reportRangeSeriesStatistics(points, selection);
      const exact = fullMetrics[key];
      if (!exact || typeof exact !== "object") return { ...preview, fullResolution: false };
      return {
        count: finite(exact.sample_count) ? Number(exact.sample_count) : preview.count,
        average: finite(exact.average) ? Number(exact.average) : null,
        minimum: finite(exact.minimum) ? Number(exact.minimum) : null,
        maximum: finite(exact.maximum) ? Number(exact.maximum) : null,
        calculation: String(exact.calculation || ""),
        fullResolution: true,
      };
    };
    rawMetrics.forEach(metric => {
      rows.push({
        key: metric.key,
        label: metric.label,
        unit: metric.unit,
        categories: metric.categories,
        ...resolvedStatistics(metric.key, metric.points),
      });
      (metric.overlays || []).forEach(overlay => {
        const overlayKey = String(overlay.key || `${metric.key}-overlay`);
        rows.push({
          key: overlayKey,
          label: `${metric.label} · ${overlay.label || overlay.key || "辅助序列"}`,
          unit: metric.unit,
          categories: metric.categories,
          ...resolvedStatistics(overlayKey, overlay.points),
        });
      });
    });
    return rows;
  }
  function reportRangeStatisticText(row, key) {
    if (!row.count || !finite(row[key])) return "—";
    if (Array.isArray(row.categories) && row.categories.length) return "状态型不适用";
    return format(row[key], row.unit);
  }
  function reportRangeCalculationLabel(row) {
    const labels = {
      time_weighted_full_resolution: "全量时间加权",
      sample_average_full_resolution: "全量样本均值",
      frame_rate_recomputed: "选区帧率重算",
      one_percent_low_recomputed: "选区 1% Low 重算",
      frame_quantile_recomputed: "选区分位值重算",
      frame_issue_ratio_recomputed: "选区异常比例重算",
      refresh_residency_weighted: "选区驻留时间加权",
      frame_stage_full_resolution: "全量链路样本均值",
    };
    return row.fullResolution
      ? labels[row.calculation] || "全量区间统计"
      : "显示点预览";
  }
  function updateReportRangeSummary() {
    const summaryNode = root.querySelector("#report-range-summary");
    const title = root.querySelector("#report-range-summary-title");
    const time = root.querySelector("#report-range-summary-time");
    const statistics = root.querySelector("#report-range-statistics");
    const note = root.querySelector("#report-range-summary-note");
    if (!summaryNode || !title || !time || !statistics || !note) return;
    const selection = selectedReportRange();
    summaryNode.hidden = !selection;
    statistics.replaceChildren();
    if (!selection) return;
    title.textContent = `${reportRangeSourceLabel(selection.sourceKey)}选区统计`;
    time.textContent = `${formatExactTime(selection.startElapsed)} – ${formatExactTime(selection.endElapsed)} · ${formatExactTime(selection.duration)}`;
    ["指标", "区间值 / 均值", "最小", "最大", "点数"].forEach((label, index) => {
      const cell = document.createElement("span");
      cell.textContent = label;
      if (index === 0) cell.className = "range-stat-name";
      statistics.appendChild(cell);
    });
    const rows = reportRangeStatisticRows(selection);
    rows.forEach(row => {
      const values = [
        `${row.label} · ${reportRangeCalculationLabel(row)}`,
        reportRangeStatisticText(row, "average"),
        reportRangeStatisticText(row, "minimum"),
        reportRangeStatisticText(row, "maximum"),
        row.count.toLocaleString("zh-CN"),
      ];
      values.forEach((value, index) => {
        const cell = document.createElement("span");
        cell.textContent = value;
        if (index === 0) cell.className = "range-stat-name";
        statistics.appendChild(cell);
      });
    });
    const fullResolutionCount = rows.filter(row => row.fullResolution).length;
    if (reportRangeFullSummary) {
      note.textContent = fullResolutionCount === rows.length
        ? `已使用服务端全量数据重算 ${fullResolutionCount} 项区间统计；删除后会再次用全量记录重建整份报告。`
        : `已使用服务端全量数据覆盖 ${fullResolutionCount} 项统计；其余项目仍为显示点预览，删除后会用全量记录重建整份报告。`;
    } else if (!reportRunName) {
      note.textContent = reportPayload.downsampled
        ? `独立 HTML 无法请求全量区间统计；当前按 ${Number(reportPayload.display_sample_count || samples.length).toLocaleString("zh-CN")} 个显示点预览。请从仪表盘打开报告以获得全量结果。`
        : "独立 HTML 当前按报告内显示点预览；请从仪表盘打开报告以请求服务端全量区间统计。";
    } else if (reportRangeSummaryError) {
      note.textContent = `全量区间统计请求失败（${reportRangeSummaryError}），当前继续显示点预览；删除后仍会用全量记录重建整份报告。`;
    } else {
      note.textContent = reportPayload.downsampled
        ? `正在准备全量统计；当前先按 ${Number(reportPayload.display_sample_count || samples.length).toLocaleString("zh-CN")} 个显示点预览。删除后会用全量 ${Number(reportPayload.raw_sample_count || 0).toLocaleString("zh-CN")} 条原始记录重建报告。`
        : "当前先按报告内显示点预览；框选固化后会请求服务端全量区间统计，删除后仍会用全量记录重建报告。";
    }
  }
  function updateReportRangeEditor(changed = "") {
    const startInput = root.querySelector("#report-range-start");
    const endInput = root.querySelector("#report-range-end");
    const startValue = root.querySelector("#report-range-start-value");
    const endValue = root.querySelector("#report-range-end-value");
    const status = root.querySelector("#report-range-status");
    const deleteButton = root.querySelector("#delete-report-range");
    if (!startInput || !endInput || !status || !deleteButton || !samples.length) return;
    const maximum = Math.max(0, samples.length - 1);
    let start = Math.max(0, Math.min(maximum, Number(startInput.value || 0)));
    let end = Math.max(0, Math.min(maximum, Number(endInput.value || 0)));
    if (changed) {
      if (changed === "start" && start >= end) {
        end = Math.min(maximum, start + 1);
        if (end <= start) start = Math.max(0, end - 1);
      } else if (changed === "end" && end <= start) {
        start = Math.max(0, end - 1);
        if (end <= start) end = Math.min(maximum, start + 1);
      }
      reportRangeTouched = true;
      reportRangeStartElapsed = Number(samples[start].elapsed_s || 0);
      reportRangeEndElapsed = Number(samples[end].elapsed_s || 0);
      reportRangeSourceKey = "range_sliders";
      reportRangeFullSummary = null;
      reportRangeSummaryError = "";
      reportRangeSummaryRequestId += 1;
      if (reportRangeSummaryTimer) window.clearTimeout(reportRangeSummaryTimer);
    } else if (reportRangeTouched) {
      start = nearestIndex(reportRangeStartElapsed);
      end = nearestIndex(reportRangeEndElapsed);
    }
    startInput.value = String(start);
    endInput.value = String(end);
    const selection = selectedReportRange();
    if (startValue) startValue.textContent = selection ? formatExactTime(selection.startElapsed) : formatExactTime(samples[start].elapsed_s);
    if (endValue) endValue.textContent = selection ? formatExactTime(selection.endElapsed) : formatExactTime(samples[end].elapsed_s);
    const canDelete = Boolean(reportRunName && selection);
    deleteButton.disabled = !canDelete;
    status.dataset.state = "";
    if (!reportRunName) {
      status.textContent = selection
        ? "独立 HTML 报告可查看选区统计；如需删除，请从仪表盘的历史报告中打开。"
        : "独立 HTML 报告保持只读；可在任意时间图上框选并查看区间统计。";
    } else if (!selection) {
      const edited = Number(reportEdits.excluded_range_count || reportExcludedRanges.length || 0);
      status.textContent = `${edited ? `已删除 ${edited} 个时间段；` : ""}在任意时间图上横向拖动框选，也可用滑块微调。`;
    } else {
      status.textContent = `已选择 ${formatExactTime(selection.startElapsed)} – ${formatExactTime(selection.endElapsed)} · 时长 ${formatExactTime(selection.duration)}；删除资格与最终统计由全量数据校验。`;
    }
    updateReportRangeSummary();
    refreshReportRangeSelectionOverlays();
    if (changed) {
      scheduleReportRangeFullSummary();
      selectSample(changed === "start" ? start : end);
    }
  }
  async function deleteSelectedReportRange() {
    const selection = selectedReportRange();
    const status = root.querySelector("#report-range-status");
    const button = root.querySelector("#delete-report-range");
    if (!selection || !status || !button || !reportRunName) return;
    const startLabel = formatExactTime(selection.startElapsed);
    const endLabel = formatExactTime(selection.endElapsed);
    const previewNote = reportPayload.downsampled
      ? "当前选区统计只是显示点预览；删除后会用全量原始记录重新计算整份报告。"
      : "删除后会用全量记录重新计算整份报告。";
    if (!window.confirm(`确认从报告中删除 ${startLabel} – ${endLabel} 的记录？原始采集包会保留，以便审计或恢复；${previewNote}`)) return;
    button.disabled = true;
    button.textContent = "正在删除并重建报告…";
    status.dataset.state = "";
    status.textContent = "正在保存排除区间并重新生成报告，请稍候。";
    try {
      const response = await fetch("/api/report/delete-range", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          run_name: reportRunName,
          start_uptime_s: Number(selection.startUptime),
          end_uptime_s: Number(selection.endUptime),
        }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      const removedSamples = Number(result.deleted_sample_count || 0);
      const removedContexts = Number(result.deleted_context_count || 0);
      status.textContent = `已从报告排除 ${removedSamples} 个主样本${removedContexts ? `、${removedContexts} 个帧/上下文点` : ""}，正在刷新报告。`;
      const nextUrl = new URL(window.location.href);
      nextUrl.searchParams.set("edited", String(Date.now()));
      window.location.replace(nextUrl.toString());
    } catch (error) {
      status.dataset.state = "error";
      status.textContent = `删除失败：${error.message || error}`;
      button.disabled = false;
    } finally {
      button.textContent = "删除选中时间段记录";
    }
  }

  root.querySelectorAll(".nav-tab").forEach(tab => tab.addEventListener("click", () => {
    const view = tab.dataset.view;
    root.querySelectorAll(".nav-tab").forEach(peer => peer.setAttribute("aria-selected", peer === tab ? "true" : "false"));
    root.querySelectorAll(".app-view").forEach(panel => { panel.hidden = panel.dataset.panel !== view; });
    window.requestAnimationFrame(() => {
      if (view === "raw") { renderRawMetricCharts(); renderFrameFlowHistory(); }
      if (view === "analysis") renderFrameStability();
    });
  }));
  root.querySelectorAll("[data-cpu-cluster]").forEach(button => button.addEventListener("click", () => {
    selectedCluster = button.dataset.cpuCluster;
    root.querySelectorAll("[data-cpu-cluster]").forEach(peer => peer.classList.toggle("active", peer === button));
    renderCpu();
  }));
  root.querySelectorAll(".test-item-row").forEach(row => row.addEventListener("click", () => {
    const start = Number(row.dataset.testStart);
    const end = Number(row.dataset.testEnd);
    if (!Number.isFinite(start) || !Number.isFinite(end)) return;
    const duration = Math.max(1, end - start);
    const padding = Math.max(5, duration * .06);
    testRange = [Math.max(0, start - padding), Math.min(maxTime(), end + padding)];
    selectSample(nearestIndex((start + end) * .5));
  }));
  const testRangeReset = root.querySelector("#test-range-reset");
  if (testRangeReset) testRangeReset.addEventListener("click", () => {
    testRange = null;
    renderTestItems();
  });
  const slider = root.querySelector("#overview-slider");
  if (slider) slider.addEventListener("input", () => selectSample(slider.value));
  const reportRangeStart = root.querySelector("#report-range-start");
  const reportRangeEnd = root.querySelector("#report-range-end");
  const deleteReportRange = root.querySelector("#delete-report-range");
  if (reportRangeStart) reportRangeStart.addEventListener("input", () => updateReportRangeEditor("start"));
  if (reportRangeEnd) reportRangeEnd.addEventListener("input", () => updateReportRangeEditor("end"));
  if (deleteReportRange) deleteReportRange.addEventListener("click", deleteSelectedReportRange);
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => { renderRawMetricCharts(); renderFrameFlowHistory(); renderFrameStability(); }, 100);
  });
  renderRawMetricGrid();
  selectSample(testMode === "performance" ? Math.max(0, samples.length - 1) : 0);
  updateReportRangeEditor();
  renderFrameStability();
  const initialView = new URLSearchParams(window.location.search).get("view") || window.location.hash.slice(1);
  const initialTab = initialView ? Array.from(root.querySelectorAll("[data-view]")).find(tab => tab.dataset.view === initialView && window.getComputedStyle(tab).display !== "none") : null;
  if (initialTab && initialView !== "raw") initialTab.click();
})();
</script>
"""


STANDALONE_STYLE = """
html { background: #111315; }
body { margin: 0; min-width: 320px; background: #111315; }
""".strip()


def _report_platform_profile(
    metadata: Dict[str, object],
    device: Dict[str, object],
) -> Dict[str, str]:
    platform = str(metadata.get("platform") or "android").lower()
    if platform == "ios":
        model = " ".join(
            str(part) for part in (device.get("brand"), device.get("model")) if part
        ) or str(device.get("product_type") or "iPhone")
        return {
            "platform": "ios",
            "platform_name": "iOS",
            "os_version": str(device.get("ios") or "未知"),
            "model": model,
            "hardware": str(
                device.get("hardware") or device.get("product_type") or "未知硬件"
            ),
            "cpu_title": "CPU 活动",
            "cpu_copy": (
                "DVT 提供整机与进程 CPU 利用率；当前公开接口未提供可可靠使用的 "
                "CPU 集群频率或 Android Power Profile 等价模型。"
            ),
            "cpu_tag_kind": "counter",
            "cpu_tag": "计数器，非 CPU 电源轨",
            "cpu_timeline_copy": "有平台级集群频率证据时才绘制；当前 iOS 采集通常留空。",
            "system_copy": (
                "周期采集 DVT sysmontap 进程 CPU、内存、磁盘计数器和相对 powerScore；"
                "sysmond、DTServiceHub 与配对服务单独标记为观察者相关进程。"
            ),
            "system_source": "DVT sysmontap 快照",
            "system_activity_title": "系统与观察者相关活动聚合",
            "priority_activity_title": "重点系统 / 观察者相关活动",
            "thermal_copy": (
                "展示 iOS 当前可观测的电池温度；未公开的热严重度、冷却设备、"
                "cpuset 与调度策略会明确留空。"
            ),
            "gpu_copy": (
                "使用 DVT Graphics 的 Device / Renderer / Tiler 利用率作为相对活动证据；"
                "它不是 GPU 电源轨，也不是应用级电能归因。"
            ),
            "attribution_copy": (
                "iOS 整机原始 SystemLoad 通道与 DVT 进程相对功耗分数分开展示；"
                "当前不把相对分数换算成 mW 或应用独占能量。"
            ),
            "attribution_tag_kind": "context",
            "attribution_tag": "相对分数 ≠ mW",
            "attribution_note": (
                '<div class="availability-note"><strong>iOS 归因边界</strong>'
                '<span>DVT powerScore 仅用于同一会话内的相对诊断。整机原始功率通道来自 '
                'DiagnosticsService PowerTelemetryData.SystemLoad，电池 I×V 另作电池流量；'
                '这些数值不可相加，也不构成单进程或独立硬件电源轨测量。</span></div>'
            ),
            "test_item_copy": (
                "按前台应用或导入测试阶段计算整机能量，并同步检查可见的进程、"
                "采集器、GPU 与电池温度证据。"
            ),
            "test_item_timeline_copy": (
                "功率、前台应用、测试项、系统活动和平台可提供的热 / 调度证据共享同一时间轴。"
            ),
            "test_item_boundary": (
                "测试项能量仅来自低频 SystemLoad 中可验证的有效放电区间；电池电流×电压另作电池流量，进程 CPU、DVT 相对功耗分数、"
                "GPU 利用率和温度只表示同期证据。多个重叠测试项不可相加，也不能当作单进程独占功耗。"
            ),
        }
    if platform == "harmony":
        model = " ".join(
            str(part) for part in (device.get("brand"), device.get("model")) if part
        ) or "HarmonyOS 设备"
        return {
            "platform": "harmony",
            "platform_name": "HarmonyOS",
            "os_version": str(device.get("harmony") or device.get("openharmony") or "未知"),
            "model": model,
            "hardware": str(device.get("soc_model") or device.get("hardware") or "未知 SoC"),
            "cpu_title": "CPU 活动与频率",
            "cpu_copy": (
                "使用 HDC 持久化 shell 采集 /proc/stat 利用率，并以较低频率读取 hidumper --cpufreq；"
                "相同最大频率的核心只按组取均值，不代表共享 Android cpufreq policy；"
                "不套用 Android Power Profile，也不推断 CPU 电源轨功耗。"
            ),
            "cpu_tag_kind": "counter",
            "cpu_tag": "HDC 实测计数器，非功率模型",
            "cpu_timeline_copy": "展示 HarmonyOS CPU 集群利用率与频率上下文；频率刷新低于电流采样。",
            "system_copy": (
                "周期采集 HarmonyOS top / ps 进程快照，并标记可见的系统更新、安装和运行时编译活动。"
            ),
            "system_source": "HDC top + ps 快照",
            "system_activity_title": "HarmonyOS 系统与内核活动聚合",
            "priority_activity_title": "更新 / 安装 / 编译活动",
            "thermal_copy": (
                "展示 ThermalService 传感器温度、PowerManager 状态与 cpufreq 能力快照；"
                "Android 热严重度、冷却设备、ActivityManager 和 ADPF 语义不适用并明确留空。"
            ),
            "gpu_copy": (
                "当前量产 HarmonyOS 未向 HDC shell 暴露可读 GPU 频率或负载节点，"
                "也没有 Android dumpsys GPU 的 UID 工作时长回退，因此不推断 GPU 活动或功耗。"
            ),
            "attribution_copy": (
                "HarmonyOS 报告以 BatteryService 电流和电压计算电池侧整机功率；"
                "进程、CPU、热传感器与前台 Ability 仅作为同期上下文，不转换为应用独占 mW。"
            ),
            "attribution_tag_kind": "context",
            "attribution_tag": "整机实测 ≠ 进程独占功耗",
            "attribution_note": (
                '<div class="availability-note"><strong>HarmonyOS 归因边界</strong>'
                '<span>Android BatteryStats、ADPF、ActivityManager 与 dumpsys GPU 在 HarmonyOS 上不存在。'
                '报告不会用 Android 名称包装 HarmonyOS 数据，也不会把进程 CPU 快照当作电能归因。</span></div>'
            ),
            "test_item_copy": (
                "按前台 Ability 或导入测试阶段计算电池侧整机能量，并同步检查进程、CPU、热传感器和电源状态。"
            ),
            "test_item_timeline_copy": (
                "功率、前台 Ability、测试项、系统活动与 HarmonyOS 热 / 电源上下文共享设备实时时钟；"
                "前台、亮灭屏与供电状态约每 5 秒复核一次。"
            ),
            "test_item_boundary": (
                "测试项能量来自 BatteryService 电流与电压的整机实测；进程 CPU、系统活动、频率与温度只表示同期证据。"
                "重叠测试项不可相加，也不能当作单应用或单硬件电源轨功耗。切换前后台、亮灭屏或插拔供电后的"
                "过渡窗口可能保留最多约 5 秒的上一状态归属。"
            ),
        }
    model = " ".join(
        str(part) for part in (device.get("brand"), device.get("model")) if part
    ) or "Android 设备"
    return {
        "platform": "android",
        "platform_name": "Android",
        "os_version": str(device.get("android") or "未知"),
        "model": model,
        "hardware": str(device.get("soc_model") or device.get("hardware") or "未知 SoC"),
        "cpu_title": "CPU 频率影响",
        "cpu_copy": "将单核利用率、cpufreq 与 Android Power Profile 电流表关联分析。",
        "cpu_tag_kind": "model",
        "cpu_tag": "模型估算，非独立电源轨",
        "cpu_timeline_copy": "频率、利用率与同设备模型功率。",
        "system_copy": "周期采集应用、Android 服务、系统原生服务、内核任务和热点线程的 CPU 快照。",
        "system_source": "top + ps 快照",
        "system_activity_title": "GC / kworker / 内核与显示活动聚合",
        "priority_activity_title": "DEX / 更新 / 安装活动",
        "thermal_copy": (
            "展示可观测的 ThermalService 级别、冷却设备、cpuset 边界、CPU Policy、"
            "ActivityManager 状态与 ADPF 会话。"
        ),
        "gpu_copy": (
            "优先使用可读的 OEM 频率/负载节点；受限时使用驱动提供的 UID 工作时长和进程内存快照。"
        ),
        "attribution_copy": "Android 模型证据与电池侧整机实测结果分开展示。",
        "attribution_tag_kind": "model",
        "attribution_tag": "不可直接相加的估算",
        "attribution_note": "",
        "test_item_copy": (
            "按每个前台测试项计算整机能量，并同步检查 GC、kworker、DEX/更新、热限制和主要进程干扰。"
        ),
        "test_item_timeline_copy": (
            "功率、前台应用、测试项、系统活动、Thermal 与 ADPF / 调度状态共享同一时间轴。"
        ),
        "test_item_boundary": (
            "测试项能量来自电池侧整机实测；进程、GC、kworker 与 DEX/更新列表示周期快照与测试窗口的时间关联。"
            "多个重叠测试项的能量不可相加，也不能当作某个进程的独占电源轨功耗。"
        ),
    }


def _report_mode_profile(
    profile: Dict[str, str],
    metadata: Dict[str, object],
) -> Dict[str, str]:
    test_mode = str(metadata.get("test_mode") or "power").strip().lower()
    result = dict(profile)
    if test_mode == "performance":
        platform = str(result.get("platform") or "android")
        capture = metadata.get("capture_configuration", {})
        capture = capture if isinstance(capture, dict) else {}
        if platform == "ios":
            result.update(
                {
                    "report_subtitle": "iOS CPU / GPU、整机原始 SystemLoad 与前台状态分析",
                    "overview_title": "iOS 性能测试概览",
                    "overview_tag": "DVT 资源遥测 + PowerTelemetry",
                    "overview_copy": (
                        "CPU、实际收到的 GPU 利用率、低频整机原始 SystemLoad、电池诊断与前台应用事件统一按设备时间对齐。"
                        "当前后端不提供通用应用 FPS、1% Low 或 Core Animation 详细帧时间戳。"
                    ),
                    "timeline_copy": (
                        "只并排展示本次实际采到的 CPU、GPU、整机原始 SystemLoad 通道、电池流量、电流、电压、温度和观察者相关进程 CPU。"
                    ),
                    "cpu_title": "iOS CPU 资源上下文",
                    "cpu_copy": (
                        "展示 DVT 归一化整机 CPU；iOS 未公开 CPU 集群频率，报告不会虚构频率或 CPU 电源轨。"
                    ),
                    "cpu_tag_kind": "counter",
                    "cpu_tag": "DVT 计数器，非 CPU 电源轨",
                    "cpu_timeline_copy": "CPU 用于解释资源压力，不换算独立功耗。",
                    "gpu_copy": (
                        "仅在实际收到 DVT Graphics 事件时展示 GPU 利用率；数据流停止后旧值不会继续冒充实时样本。"
                    ),
                    "test_item_copy": (
                        "按导入阶段或真实前台应用事件聚合 CPU/GPU、整机原始 SystemLoad 和温度；不生成 FPS、1% Low 或渲染阶段结论。"
                    ),
                    "test_item_timeline_copy": (
                        "CPU/GPU、前台应用、测试项、SystemLoad、电池流量与电池温度共享同一设备时间轴。"
                    ),
                    "test_item_boundary": (
                        "PowerTelemetry SystemLoad 通常约 20 秒更新；观察者相关进程 CPU 是 sysmond、DTServiceHub 与配对服务同期活动上界，"
                        "包含本底活动，不能当作工具造成的净增量。"
                    ),
                }
            )
        elif platform == "harmony":
            smartperf = str(capture.get("backend") or "") == "harmony_smartperf"
            result.update(
                {
                    "report_subtitle": "HarmonyOS 帧节奏、资源与温度分析",
                    "overview_title": "HarmonyOS 性能测试概览",
                    "overview_tag": "SmartPerf 应用帧" if smartperf else "RenderService 合成上下文",
                    "overview_copy": (
                        "SmartPerf 使用目标处于前台时的应用 FPS 与原始 jitter；CPU/GPU/DDR、温度和电池侧数据按同一设备时间域对齐。"
                        if smartperf
                        else "原生后端只保留亮屏期间可验证的 RenderService 合成节奏、CPU 频率、温度和电池侧上下文；不会冒充目标应用 FPS。"
                    ),
                    "timeline_copy": (
                        "只展示实际采到的帧节奏、CPU/GPU/DDR、温度与电池侧原始数据；USB 充电流量不会解释为设备耗电。"
                    ),
                    "cpu_title": "CPU 活动与频率上下文",
                    "cpu_copy": (
                        "HarmonyOS 频率按最大频率相同的核心分组取均值，不代表这些核心共享 Android cpufreq policy。"
                    ),
                    "cpu_tag_kind": "counter",
                    "cpu_tag": "HDC / SmartPerf 计数器",
                    "cpu_timeline_copy": "CPU 频率和利用率用于解释帧节奏，不进行 CPU 功耗归因。",
                    "gpu_copy": (
                        "SmartPerf 返回 GPU/DDR 时才展示；原生 HDC 后端没有这些会话时间序列，不进行补值或推断。"
                    ),
                    "test_item_copy": (
                        "按目标前台区间聚合 SmartPerf 应用 FPS、1% Low、P95/P99 jitter、资源与温度上下文。"
                        if smartperf
                        else "按测试阶段聚合系统级合成节奏、CPU 与温度上下文，并明确它不是目标应用独立 FPS。"
                    ),
                    "test_item_timeline_copy": (
                        "帧节奏、前台 Ability、测试项、资源、温度传感器和电池侧数据共享同一设备时间域；"
                        "前台、亮灭屏与供电状态约每 5 秒复核一次。"
                    ),
                    "test_item_boundary": (
                        "SmartPerf jitter 可用于帧间隔统计，但不提供 Android RenderThread/BufferQueue/GPU 阶段拆分；"
                        "状态切换后的归属可能有最多约 5 秒过渡延迟。"
                        if smartperf
                        else "原生 RenderService 是系统级合成上下文；没有目标绑定时不生成应用 1% Low 或渲染阶段结论；"
                        "状态切换后的归属可能有最多约 5 秒过渡延迟。"
                    ),
                }
            )
        else:
            result.update(
                {
                "report_subtitle": "游戏帧表现、渲染链路与资源调度分析",
                "overview_title": "性能测试概览",
                "overview_tag": "帧计数器 + 电池侧功率记录",
                "overview_copy": (
                    "帧表现使用前台窗口 / 合成器计数器；CPU、GPU、调度与电池侧功率统一使用设备 uptime 对齐。"
                ),
                "timeline_copy": (
                    "帧率窗口、CPU/GPU、可用资源、调度与逐样本电池侧功率位于同一设备时间轴。"
                ),
                "cpu_title": "CPU 调度与频率上下文",
                "cpu_copy": (
                    "CPU 只保留整机总负载与核心组频率；核心组表展示核心编号、频率范围和驻留，不重复罗列分组负载或模型功率。"
                ),
                "cpu_tag_kind": "counter",
                "cpu_tag": "资源计数器，不换算 CPU 电源轨",
                "cpu_timeline_copy": "频率与利用率用于解释帧延迟，不进行 CPU 功耗归因。",
                "gpu_copy": (
                    "展示可读的 GPU 频率与负载，用于判断 GPU 饱和、渲染分辨率和带宽压力；不展示 UID 工作时长或进程功耗归因。"
                ),
                "test_item_copy": (
                    "按导入测试阶段或前台 Activity 聚合平均 FPS、1% Low、P95/P99、异常帧、渲染阶段、资源调度与热状态。"
                ),
                "test_item_timeline_copy": (
                    "帧表现、前台窗口、测试项、CPU/GPU/内存资源、热状态和调度证据共享同一时间轴。"
                ),
                "test_item_boundary": (
                    "详细 framestats 可用时用于阶段和慢帧分析；否则使用周期帧计数窗口给出保守指标。"
                    "测试项只展示窗口平均电池侧功率，不拆分到进程、UID、Wakelock 或硬件组件。"
                ),
                }
            )
    else:
        result.update(
            {
                "report_subtitle": "续航功耗、任务压力与系统设置分析",
                "overview_title": "功耗测试概览",
                "overview_tag": "电池侧整机实测",
                "overview_copy": (
                    "电流、电压、任务负载、CPU/GPU、可用频率和设置上下文统一使用设备 uptime 时间轴。"
                ),
                "timeline_copy": (
                    "整机电流功率、任务负载、CPU/GPU、可用频率与系统活动位于同一时间轴。"
                ),
            }
        )
    return result


def build_report_fragment(bundle: Dict[str, object]) -> str:
    bundle = _report_bundle(bundle)
    metadata = bundle.get("metadata", {})
    analysis = bundle.get("analysis", {})
    samples = bundle.get("samples", [])
    summary = analysis.get("summary", {}) if isinstance(analysis, dict) else {}
    device = metadata.get("device", {}) if isinstance(metadata, dict) else {}
    analysis_mode = analysis.get("test_mode") if isinstance(analysis, dict) else None
    performance_hint = analysis.get("performance", {}) if isinstance(analysis, dict) else {}
    performance_hint = performance_hint if isinstance(performance_hint, dict) else {}
    inferred_mode = "performance" if (
        performance_hint.get("available") is True
        or isinstance(performance_hint.get("frame_sample_count"), (int, float))
        and float(performance_hint.get("frame_sample_count") or 0.0) > 0
    ) else "power"
    test_mode = str(
        analysis_mode or metadata.get("test_mode") or inferred_mode
    ).strip().lower()
    if test_mode not in {"power", "performance"}:
        test_mode = "power"
    profile_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    profile_metadata["test_mode"] = test_mode
    profile = _report_mode_profile(
        _report_platform_profile(profile_metadata, device),
        profile_metadata,
    )
    platform = profile["platform"]
    target = metadata.get("target_package")
    if not target:
        target = "多应用会话" if metadata.get("session_mode") else metadata.get("foreground_package") or "未指定目标"
    cpu_rows, residency_rows, selectors = _cpu_rows(analysis)
    gpu_status, gpu_metric, gpu_uid_rows, gpu_memory_rows = _gpu_content(analysis, platform)
    priority_status, priority_rows = _priority_activity_content(analysis, platform)
    capture_start = metadata.get("capture_start", {}) if isinstance(metadata, dict) else {}
    capture_start = capture_start if isinstance(capture_start, dict) else {}
    capture_start_note = ""
    if capture_start:
        context_labels = {
            "desktop": "桌面 / 主屏幕",
            "app": "应用内",
            "other": "其他场景",
            "unknown": "不确定",
        }
        expected = context_labels.get(
            str(capture_start.get("expected_context") or "unknown"),
            str(capture_start.get("expected_context") or "不确定"),
        )
        note = str(capture_start.get("note") or "无备注")
        observed = str(capture_start.get("observed_foreground_package") or metadata.get("foreground_package") or "未知")
        capture_start_note = (
            '<div class="availability-note"><strong>独立采集起点</strong>'
            f'<span>预期场景：{_escape(expected)} · 实际前台：{_escape(observed)} · 备注：{_escape(note)}。'
            "Profiler 与 BTR2 可先后启动，测试项以导入日志对齐区间为准。</span></div>"
        )
    warnings = _report_warning_items(analysis, test_mode)
    warning_section = ""
    if warnings:
        warning_section = (
            '<section class="analysis-section"><h2>采集警告</h2><ul class="warning-list">'
            + "".join(f"<li>{_escape(item)}</li>" for item in warnings)
            + "</ul></section>"
        )
    gpu_analysis = analysis.get("gpu", {}) if isinstance(analysis, dict) else {}
    gpu_analysis = gpu_analysis if isinstance(gpu_analysis, dict) else {}
    gpu_metric_name = (
        "gpu_frequency_mhz"
        if gpu_analysis.get("frequency_available")
        else "gpu_load_pct"
        if gpu_analysis.get("load_available")
        else None
    )
    gpu_button = (
        f'<button type="button" class="segment-button" data-overview-metric="{gpu_metric_name}">GPU</button>'
        if gpu_metric_name
        else ""
    )
    gpu_page_available = _gpu_report_available(analysis, test_mode)
    gpu_nav = (
        '<button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="gpu">GPU</button>'
        if gpu_page_available
        else ""
    )
    gpu_section = ""
    if gpu_page_available:
        gpu_chart_section = (
            '<section class="analysis-section"><div class="chart-surface" id="gpu-chart-surface">'
            '<svg id="gpu-chart" role="img" aria-label="GPU telemetry timeline"></svg></div></section>'
            if _gpu_live_available(analysis)
            else ""
        )
        gpu_uid_section = ""
        gpu_memory_section = ""
        if test_mode == "power":
            work_by_uid = gpu_analysis.get("work_by_uid", [])
            if isinstance(work_by_uid, list) and work_by_uid:
                gpu_uid_section = (
                    '<section class="analysis-section"><h2>按 UID 的 GPU 工作</h2>'
                    '<div class="data-table-wrap"><table><thead><tr><th>包名 / UID</th><th>活跃时长</th>'
                    '<th>运行占比</th><th>来源</th></tr></thead><tbody>'
                    + gpu_uid_rows
                    + "</tbody></table></div></section>"
                )
            memory = gpu_analysis.get("memory", {})
            memory = memory if isinstance(memory, dict) else {}
            process_memory = memory.get("processes", [])
            if isinstance(process_memory, list) and process_memory:
                gpu_memory_section = (
                    '<section class="analysis-section"><h2>GPU 进程内存快照</h2>'
                    '<div class="data-table-wrap"><table><thead><tr><th>进程</th><th>PID</th>'
                    '<th>内存</th><th>占 GPU 总量</th></tr></thead><tbody>'
                    + gpu_memory_rows
                    + "</tbody></table></div></section>"
                )
        gpu_section = (
            '<section class="app-view" data-panel="gpu" hidden>'
            '<div class="view-heading"><div><h1>GPU 证据</h1>'
            f'<p>{_escape(profile["gpu_copy"])}</p></div></div>'
            f'<section class="analysis-section"><div class="status-line">{gpu_status}</div></section>'
            f'<div>{gpu_metric}</div>'
            + gpu_chart_section
            + gpu_uid_section
            + gpu_memory_section
            + "</section>"
        )
    payload = {
        "metadata": metadata,
        "analysis": analysis,
        "samples": samples,
        "contexts": bundle.get("contexts", []),
        "events": bundle.get("events", []),
    }
    replacements = {
        "@@TEST_MODE@@": _escape(test_mode),
        "@@REPORT_SUBTITLE@@": _escape(profile["report_subtitle"]),
        "@@OVERVIEW_TITLE@@": _escape(profile["overview_title"]),
        "@@OVERVIEW_TAG@@": _escape(profile["overview_tag"]),
        "@@OVERVIEW_COPY@@": _escape(profile["overview_copy"]),
        "@@TIMELINE_COPY@@": _escape(profile["timeline_copy"]),
        "@@TITLE@@": _escape(metadata.get("title") or APP_NAME),
        "@@TARGET@@": _escape(target),
        "@@DURATION@@": _number(summary.get("duration_s"), 1, "0"),
        "@@SAMPLES@@": _escape(summary.get("sample_count") or len(samples)),
        "@@DEVICE@@": _escape(profile["model"]),
        "@@PLATFORM_NAME@@": _escape(profile["platform_name"]),
        "@@OS_VERSION@@": _escape(profile["os_version"]),
        "@@HARDWARE@@": _escape(profile["hardware"]),
        "@@GENERATED@@": _escape(metadata.get("generated_at", "")),
        "@@SUMMARY_CARDS@@": _summary_cards(summary),
        "@@PERFORMANCE_CARDS@@": _performance_cards(analysis),
        "@@REFRESH_RESIDENCY_ROWS@@": _refresh_residency_rows(analysis),
        "@@PERFORMANCE_CONTEXT_ROWS@@": _performance_context_rows(analysis),
        "@@OVERVIEW_PERFORMANCE_CONTEXT_SECTION@@": _performance_context_section(
            analysis,
            "性能上下文",
        ),
        "@@PERFORMANCE_CONTEXT_SECTIONS@@": _performance_context_sections(analysis),
        "@@PERFORMANCE_POWER_RECORDING@@": _performance_power_recording(analysis),
        "@@ANALYSIS_CONCLUSION_SECTIONS@@": _analysis_conclusion_sections(
            analysis,
            test_mode,
        ),
        "@@BRIGHTNESS_THROTTLING_SECTION@@": _brightness_throttling_section(analysis),
        "@@POWER_PRESSURE_SECTIONS@@": _power_pressure_sections(analysis),
        "@@POWER_PRESSURE_DRIVER_ROWS@@": _power_pressure_driver_rows(analysis),
        "@@POWER_PRESSURE_TASK_ROWS@@": _power_pressure_task_rows(analysis),
        "@@RUNTIME_SETTING_ROWS@@": _runtime_setting_rows(analysis),
        "@@MEMORY_PRESSURE_SUMMARY@@": _memory_pressure_summary(analysis),
        "@@FRAME_FLOW_ROWS@@": _frame_flow_rows(analysis),
        "@@FRAME_FLOW_VISUAL@@": _frame_flow_visual(analysis),
        "@@FRAME_FLOW_HISTORY_SECTION@@": _frame_flow_history_section(analysis),
        "@@FRAME_FLOW_EVIDENCE@@": _frame_flow_evidence(analysis),
        "@@FRAME_STABILITY_SECTION@@": _frame_stability_section(analysis),
        "@@RENDER_PIPELINE_ROWS@@": _render_pipeline_rows(analysis),
        "@@RENDER_PIPELINE_SECTION@@": _render_pipeline_section(analysis),
        "@@SLOW_FRAME_ROWS@@": _slow_frame_rows(analysis),
        "@@SLOW_FRAME_SECTION@@": _slow_frame_section(analysis),
        "@@RENDER_THREAD_ROWS@@": _render_thread_rows(analysis),
        "@@PIPELINE_RENDER_THREAD_SECTION@@": _render_thread_section(analysis),
        "@@SYSTEM_RENDER_THREAD_SECTION@@": _render_thread_section(
            analysis,
            "RenderThread / SurfaceFlinger / Composer",
        ),
        "@@PERFORMANCE_PROCESS_ROWS@@": _performance_process_rows(analysis),
        "@@PERFORMANCE_PROCESS_SECTION@@": _performance_process_section(analysis),
        "@@PERFORMANCE_INTERFERENCE_STATUS@@": _performance_interference_status(analysis),
        "@@GPU_METRIC_BUTTON@@": gpu_button,
        "@@GPU_NAV@@": gpu_nav,
        "@@GPU_SECTION@@": gpu_section,
        "@@SLIDER_MAX@@": str(max(0, len(samples) - 1)),
        "@@CPU_ROWS@@": cpu_rows,
        "@@PERFORMANCE_CPU_ROWS@@": _performance_cpu_rows(analysis),
        "@@CPU_TITLE@@": _escape(profile["cpu_title"]),
        "@@CPU_COPY@@": _escape(profile["cpu_copy"]),
        "@@CPU_TAG_KIND@@": _escape(profile["cpu_tag_kind"]),
        "@@CPU_TAG@@": _escape(profile["cpu_tag"]),
        "@@CPU_TIMELINE_COPY@@": _escape(profile["cpu_timeline_copy"]),
        "@@CPU_SELECTORS@@": selectors,
        "@@RESIDENCY_ROWS@@": residency_rows,
        "@@PROCESS_ROWS@@": _process_rows(analysis),
        "@@CPU_PROCESS_SECTION@@": _cpu_process_section(analysis),
        "@@PRIORITY_STATUS@@": priority_status,
        "@@PRIORITY_ROWS@@": priority_rows,
        "@@SYSTEM_COPY@@": _escape(profile["system_copy"]),
        "@@SYSTEM_SOURCE@@": _escape(profile["system_source"]),
        "@@SYSTEM_ACTIVITY_TITLE@@": _escape(profile["system_activity_title"]),
        "@@PRIORITY_ACTIVITY_TITLE@@": _escape(profile["priority_activity_title"]),
        "@@ACTIVITY_GROUP_ROWS@@": _activity_group_rows(analysis),
        "@@SYSTEM_PROCESS_ROWS@@": _system_process_rows(analysis),
        "@@SYSTEM_THREAD_ROWS@@": _system_thread_rows(analysis),
        "@@THERMAL_SENSOR_ROWS@@": _thermal_sensor_rows(analysis),
        "@@THERMAL_COPY@@": _escape(profile["thermal_copy"]),
        "@@COOLING_ROWS@@": _cooling_rows(analysis),
        "@@CPUSET_ROWS@@": _cpuset_rows(analysis),
        "@@SCHEDULER_POLICY_ROWS@@": _scheduler_policy_rows(analysis),
        "@@HINT_SESSION_ROWS@@": _hint_session_rows(analysis),
        "@@SCHEDULER_PROCESS_ROWS@@": _scheduler_process_rows(analysis),
        "@@FINDINGS@@": _finding_rows(analysis),
        "@@GPU_STATUS@@": gpu_status,
        "@@GPU_COPY@@": _escape(profile["gpu_copy"]),
        "@@GPU_METRIC@@": gpu_metric,
        "@@GPU_UID_ROWS@@": gpu_uid_rows,
        "@@GPU_MEMORY_ROWS@@": gpu_memory_rows,
        "@@COMPONENT_ROWS@@": _component_rows(analysis),
        "@@UID_ROWS@@": _uid_rows(analysis),
        "@@WAKELOCK_ROWS@@": _wakelock_rows(analysis),
        "@@ATTRIBUTION_COPY@@": _escape(profile["attribution_copy"]),
        "@@ATTRIBUTION_TAG_KIND@@": _escape(profile["attribution_tag_kind"]),
        "@@ATTRIBUTION_TAG@@": _escape(profile["attribution_tag"]),
        "@@ATTRIBUTION_NOTE@@": profile["attribution_note"],
        "@@APPLICATION_ROWS@@": _application_rows(analysis),
        "@@TRANSITION_ROWS@@": _transition_rows(analysis),
        "@@PHASE_ROWS@@": _phase_rows(analysis),
        "@@EVENT_ROWS@@": _event_rows(analysis),
        "@@WINDOW_ROWS@@": _window_rows(analysis),
        "@@TEST_ITEM_STATUS@@": _test_item_status(analysis),
        "@@TEST_ITEM_COPY@@": _escape(profile["test_item_copy"]),
        "@@TEST_ITEM_TIMELINE_COPY@@": _escape(profile["test_item_timeline_copy"]),
        "@@TEST_ITEM_BOUNDARY@@": _escape(profile["test_item_boundary"]),
        "@@CAPTURE_START_NOTE@@": capture_start_note,
        "@@TEST_ITEM_ROWS@@": _test_item_rows(analysis),
        "@@TEST_ITEM_SPAN_ROWS@@": _test_item_span_rows(analysis),
        "@@PERFORMANCE_TEST_ITEM_HEADERS@@": _performance_test_item_headers(analysis),
        "@@PERFORMANCE_TEST_ITEM_ROWS@@": _performance_test_item_rows(analysis),
        "@@PERFORMANCE_TEST_ITEM_SPAN_ROWS@@": _performance_test_item_span_rows(analysis),
        "@@APP_COVERAGE@@": _number(
            analysis.get("applications", {}).get("coverage_pct")
            if isinstance(analysis.get("applications"), dict)
            else None,
            1,
            "0",
        ),
        "@@SOURCE_ROWS@@": _source_rows(
            analysis,
            samples if isinstance(samples, list) else [],
            bundle.get("contexts", []) if isinstance(bundle.get("contexts"), list) else [],
        ),
        "@@CAPTURE_CONFIGURATION_ROWS@@": _capture_configuration_rows(metadata),
        "@@ANALYSIS_COVERAGE_SECTION@@": _analysis_coverage_section(
            analysis,
            test_mode,
            gpu_page_available,
        ),
        "@@WARNING_SECTION@@": warning_section,
        "@@METADATA@@": _escape(json.dumps(metadata, ensure_ascii=False, indent=2)),
        "@@DATA@@": _json_for_script(payload),
    }
    fragment = REPORT_FRAGMENT
    for key, value in replacements.items():
        fragment = fragment.replace(key, value)
    return fragment.strip()


def build_standalone_html(fragment: str, title: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>{STANDALONE_STYLE}</style>
</head>
<body>
{fragment}
</body>
</html>
"""


def write_report_files(output_dir: Path, bundle: Dict[str, object]) -> Tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    fragment = build_report_fragment(bundle)
    title = str(bundle.get("metadata", {}).get("title") or APP_NAME)
    fragment_path = output_dir / "report-fragment.html"
    report_path = output_dir / "report.html"
    fragment_path.write_text(fragment, encoding="utf-8")
    report_path.write_text(build_standalone_html(fragment, title), encoding="utf-8")
    return report_path, fragment_path
