from __future__ import annotations

import copy
import html
import json
from pathlib import Path
from typing import Dict, List, Tuple

from .models import APP_NAME


SOURCE_KIND_LABELS = {
    "measured": "实测",
    "measured counters": "实测计数器",
    "counter": "计数器",
    "driver": "驱动",
    "model": "模型",
    "context": "上下文",
    "diagnostic score": "诊断分数",
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
    "Whole-device battery power": "整机电池功率",
    "Battery current and voltage": "电池电流与电压",
    "CPU and process activity": "CPU 与进程活动",
    "Relative process power score": "进程相对功耗分数",
    "System processes and collector overhead": "系统进程与采集器开销",
    "Battery temperature": "电池温度",
    "Battery current, voltage and temperature": "电池电流、电压与温度",
    "CPU utilization": "CPU 利用率",
    "CPU frequency": "CPU 频率",
    "Foreground application and screen state": "前台应用与屏幕状态",
    "System processes": "系统进程",
    "Thermal sensors": "热传感器",
    "Power and scheduler context": "电源与调度上下文",
    "Battery current": "电池电流",
    "Battery voltage": "电池电压",
    "CPU utilization/frequency": "CPU 利用率 / 频率",
    "CPU frequency impact": "CPU 频率影响",
    "GPU activity": "GPU 活动",
    "Component/app attribution": "组件 / 应用归因",
    "Foreground application": "前台应用",
    "Test phases and actions": "测试阶段与动作",
    "Per-test power and system interference": "分测试项功耗与系统干扰",
    "System processes and hot threads": "系统进程与热点线程",
    "Thermal severity, sensors and cooling devices": "热级别、传感器与冷却设备",
    "cpuset, process state and ADPF hints": "cpuset、进程状态与 ADPF Hint",
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
    "Imported timestamped logs aligned to HarmonyOS device realtime": "按 HarmonyOS 设备实时时钟对齐的外部日志",
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
    power_context = f"P95 {float(summary.get('p95_power_mw') or 0.0) / 1000.0:.3f} W"
    if any(str(item).startswith("ios_") for item in power_sources):
        maximum_age = summary.get("maximum_power_sample_age_s")
        power_context += (
            f" · 最大样本年龄 {_number(maximum_age)} s"
            if isinstance(maximum_age, (int, float))
            else " · iOS 物理功率低频刷新"
        )
    cpu_context = f"峰值 {float(summary.get('maximum_cpu_pct') or 0.0):.1f}%"
    collector_cpu = summary.get("average_collector_cpu_pct")
    if isinstance(collector_cpu, (int, float)):
        cpu_context += f" · 采集器 {float(collector_cpu):.1f}%"
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
            "平均功率",
            f"{float(summary.get('average_power_mw') or 0.0) / 1000.0:.3f}",
            "W",
            power_context,
            "measured",
        ),
        (
            "电池电流",
            f"{float(summary.get('average_current_ma') or 0.0):.1f}",
            "mA",
            current_context,
            "measured",
        ),
        (
            "电池电压",
            f"{float(summary.get('average_voltage_mv') or 0.0) / 1000.0:.3f}",
            "V",
            f"{float(summary.get('energy_per_minute_mwh') or 0.0):.2f} mWh/min",
            "measured",
        ),
        (
            "CPU 利用率",
            f"{float(summary.get('average_cpu_pct') or 0.0):.1f}",
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
    rows = [
        ("前台窗口", performance.get("foreground_window_name") or "—", f"Window #{performance.get('foreground_window_id') or '—'}"),
        ("显示", resolution, f"亮度原始值 {_number(performance.get('brightness_raw'), 0)}"),
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
            '<section class="analysis-section"><h2>资源压力驱动</h2>'
            '<div class="data-table-wrap"><table><thead><tr><th>资源</th><th>功率相关系数</th>'
            '<th>方向</th><th>样本数</th><th>解释</th></tr></thead><tbody>'
            + _power_pressure_driver_rows(analysis)
            + "</tbody></table></div></section>"
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
    if settings.get("rows"):
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


def _performance_interference_status(analysis: Dict[str, object]) -> str:
    render = analysis.get("render_performance", {})
    render = render if isinstance(render, dict) else {}
    bottlenecks = render.get("bottlenecks", [])
    bottlenecks = bottlenecks if isinstance(bottlenecks, list) else []
    if not bottlenecks:
        return (
            '<div class="priority-callout"><span class="status-dot good"></span><div>'
            '<strong>未形成明确的帧延迟瓶颈结论</strong>'
            '<span>继续结合详细 framestats、RenderThread / SurfaceFlinger 热点、GPU 饱和、调度与热限制检查。</span>'
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


def _performance_test_item_headers(analysis: Dict[str, object]) -> str:
    labels = [
        "测试项",
        "时长",
        "平均 FPS",
        "1% Low",
        "P95 / P99",
        "异常帧",
        "主要延迟阶段",
        "CPU 平均 / 峰值",
        "GPU 平均 / 峰值",
    ]
    if _performance_memory_frequency_available(analysis):
        labels.append("内存频率 平均 / P95")
    labels.extend(["热限制", "整机功耗记录", "置信度"])
    return "".join(f"<th>{_escape(label)}</th>" for label in labels)


def _performance_test_item_rows(analysis: Dict[str, object]) -> str:
    test_items = analysis.get("test_items", {})
    test_items = test_items if isinstance(test_items, dict) else {}
    show_memory_frequency = _performance_memory_frequency_available(analysis)
    rows = []
    for item in test_items.get("rows", []) if isinstance(test_items.get("rows"), list) else []:
        if not isinstance(item, dict):
            continue
        windows = item.get("windows", [])
        windows = windows if isinstance(windows, list) else []
        first_start = windows[0].get("start_elapsed_s") if windows and isinstance(windows[0], dict) else 0
        last_end = windows[-1].get("end_elapsed_s") if windows and isinstance(windows[-1], dict) else first_start
        dominant = item.get("dominant_stage", {})
        dominant = dominant if isinstance(dominant, dict) else {}
        memory_cell = (
            f'<td>{_number(item.get("average_memory_frequency_mhz"), 0)} / '
            f'{_number(item.get("p95_memory_frequency_mhz"), 0)} MHz</td>'
            if show_memory_frequency
            else ""
        )
        rows.append(
            f'<tr class="test-item-row" data-test-start="{_number(first_start, 3, "0")}" data-test-end="{_number(last_end, 3, "0")}">'
            f'<td><strong>{_escape(item.get("name"))}</strong><span class="cell-sub">{_escape(item.get("phase"))} · {int(item.get("occurrence_count") or 0)} 次</span></td>'
            f'<td>{_number(item.get("duration_s"), 1)} s</td>'
            f'<td>{_number(item.get("average_fps"), 1)} FPS</td>'
            f'<td>{_number(item.get("one_percent_low_fps"), 1)} FPS</td>'
            f'<td>{_number(item.get("frame_p95_ms"), 2)} / {_number(item.get("frame_p99_ms"), 2)} ms</td>'
            f'<td>{_number(item.get("frame_issue_pct"), 2)}%<span class="cell-sub">{int(item.get("frame_issue_count") or 0)} 帧</span></td>'
            f'<td>{_escape(dominant.get("label") or "—")}<span class="cell-sub">P95 {_number(dominant.get("p95_ms"), 2)} ms</span></td>'
            f'<td>{_number(item.get("average_cpu_pct"))}% / {_number(item.get("maximum_cpu_pct"))}%</td>'
            f'<td>{_number(item.get("average_gpu_load_pct"))}% / {_number(item.get("maximum_gpu_load_pct"))}%</td>'
            f'{memory_cell}'
            f'<td>{"是" if item.get("throttling_observed") else "否"}<span class="cell-sub">{_number(item.get("maximum_temperature_c"))} °C</span></td>'
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
        if not isinstance(item, dict):
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
        premium = cluster.get("frequency_premium_mw")
        correlation = cluster.get("measured_power_correlation")
        table_rows.append(
            "<tr>"
            f'<td><strong>{_escape(label)}</strong><span class="cell-sub">CPU { _escape(cores) }</span></td>'
            f'<td>{_number(cluster.get("average_load_pct"))}%</td>'
            f'<td>{_number(cluster.get("load_weighted_mhz"), 0)} MHz</td>'
            f'<td>{_number(cluster.get("maximum_mhz"), 0)} / {_number(cluster.get("hardware_max_mhz"), 0)} MHz</td>'
            f'<td>{_number(cluster.get("modeled_power_mw"))} mW</td>'
            f'<td>{_number(premium)} mW</td>'
            f'<td>{_number(correlation, 2)}</td>'
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
        table_rows.append('<tr><td colspan="7" class="empty-cell">CPU 集群数据不可用。</td></tr>')
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
            f'<td>{_number(cluster.get("average_load_pct"))}%</td>'
            f'<td>{_number(cluster.get("load_weighted_mhz"), 0)} MHz</td>'
            f'<td>{_number(cluster.get("maximum_mhz"), 0)} / {_number(cluster.get("hardware_max_mhz"), 0)} MHz</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="4" class="empty-cell">CPU 集群数据不可用。</td></tr>'


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
                f'<span>已观察到 {len(monitored)} 个受监控进程；相对功耗分数与整机物理功率分开解释。</span>'
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


def _source_rows(analysis: Dict[str, object]) -> str:
    return "".join(
        "<tr>"
        f'<td><strong>{_escape(_display_label(item.get("metric"), METRIC_LABELS))}</strong></td>'
        f'<td>{_escape(_display_label(item.get("source"), SOURCE_LABELS))}</td>'
        f'<td><span class="source-tag {_escape(item.get("kind", "context"))}">{_escape(_display_label(item.get("kind"), SOURCE_KIND_LABELS))}</span></td>'
        "</tr>"
        for item in analysis.get("data_sources", [])
    )


def _capture_configuration_rows(metadata: Dict[str, object]) -> str:
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
            enabled = bool(item.get("enabled"))
            rows.append(
                "<tr>"
                f"<td><strong>{_escape(item.get('label') or item.get('key'))}</strong>"
                f"<span class=\"cell-sub\">{_escape(item.get('description') or '')}</span></td>"
                f"<td><span class=\"source-tag {'measured' if enabled else 'context'}\">"
                f"{'启用' if enabled else '关闭'}</span></td>"
                f"<td>{_escape(overhead_labels.get(str(item.get('overhead')), item.get('overhead') or '--'))}</td>"
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


def _lttb_indices(samples: List[Dict[str, object]], threshold: int) -> List[int]:
    count = len(samples)
    if threshold >= count or threshold < 3:
        return list(range(count))
    every = (count - 2) / (threshold - 2)
    selected = [0]
    anchor = 0
    for bucket in range(threshold - 2):
        average_start = int((bucket + 1) * every) + 1
        average_end = min(int((bucket + 2) * every) + 1, count)
        if average_start >= count:
            average_start = count - 1
        average_range = samples[average_start:average_end] or [samples[-1]]
        average_x = sum(float(item.get("elapsed_s") or 0.0) for item in average_range) / len(average_range)
        average_y = sum(float(item.get("power_mw") or 0.0) for item in average_range) / len(average_range)
        range_start = int(bucket * every) + 1
        range_end = min(int((bucket + 1) * every) + 1, count - 1)
        anchor_x = float(samples[anchor].get("elapsed_s") or 0.0)
        anchor_y = float(samples[anchor].get("power_mw") or 0.0)
        maximum_area = -1.0
        next_anchor = range_start
        for index in range(range_start, max(range_start + 1, range_end)):
            point_x = float(samples[index].get("elapsed_s") or 0.0)
            point_y = float(samples[index].get("power_mw") or 0.0)
            area = abs(
                (anchor_x - average_x) * (point_y - anchor_y)
                - (anchor_x - point_x) * (average_y - anchor_y)
            )
            if area > maximum_area:
                maximum_area = area
                next_anchor = index
        selected.append(next_anchor)
        anchor = next_anchor
    selected.append(count - 1)
    return selected


def _report_bundle(bundle: Dict[str, object], threshold: int = 1200) -> Dict[str, object]:
    prepared = copy.deepcopy(bundle)
    samples = prepared.get("samples", [])
    if not isinstance(samples, list) or len(samples) <= threshold:
        return prepared
    indices = _lttb_indices(samples, threshold)
    prepared["samples"] = [samples[index] for index in indices]
    analysis = prepared.get("analysis", {})
    if isinstance(analysis, dict):
        cpu = analysis.get("cpu", {})
        if isinstance(cpu, dict):
            timeline = cpu.get("timeline", [])
            if isinstance(timeline, list) and len(timeline) == len(samples):
                cpu["timeline"] = [timeline[index] for index in indices]
        analysis["report_payload"] = {
            "raw_sample_count": len(samples),
            "display_sample_count": len(indices),
            "downsample_method": "largest-triangle-three-buckets on measured power",
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
    #mobile-profiler .residency-row { grid-template-columns: 1fr; }
    #mobile-profiler .residency-values { grid-column: 1; }
    #mobile-profiler .contributor-row { grid-template-columns: minmax(0, 1fr) 65px; }
    #mobile-profiler .contributor-row .bar-track { grid-column: 1 / -1; grid-row: 2; }
  }
  @media (max-width: 440px) {
    #mobile-profiler .metric-grid { grid-template-columns: 1fr; }
    #mobile-profiler .device-block { width: 100%; justify-content: flex-start; text-align: left; }
    #mobile-profiler .metric-value { font-size: 22px; }
    #mobile-profiler .sample-detail { display: grid; grid-template-columns: 1fr; }
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
    <nav class="side-tabs" role="tablist" aria-label="报告页面">
      <button type="button" class="nav-tab" role="tab" aria-selected="true" data-view="overview">概览</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="timeline">时间线</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="flow" data-report-only="power">测试流程</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="test-items">测试项分析</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="pressure" data-report-only="power">功耗压力</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="pipeline" data-report-only="performance">渲染链路</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="applications" data-report-only="power">应用</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="cpu">CPU</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="system" data-report-only="performance">性能上下文</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="gpu">GPU</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="attribution" data-report-only="power">功耗归因</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="data">数据质量</button>
    </nav>
    <main class="app-content">
      <section class="app-view" data-panel="overview">
        <div class="view-heading"><div><h1>@@OVERVIEW_TITLE@@</h1><p>@@GENERATED@@</p></div><span class="source-tag measured">@@OVERVIEW_TAG@@</span></div>
        <div class="metric-grid" data-report-only="power">@@SUMMARY_CARDS@@</div>
        <div class="metric-grid" data-report-only="performance">@@PERFORMANCE_CARDS@@</div>
        <div data-report-only="performance">@@PERFORMANCE_POWER_RECORDING@@</div>
        <section class="analysis-section">
          <div class="chart-toolbar">
            <div><h2>实测遥测</h2><p class="section-copy">@@OVERVIEW_COPY@@</p></div>
            <div class="segment-control" aria-label="概览指标">
              <button type="button" class="segment-button active" data-overview-metric="power_mw" data-report-only="power">功率</button>
              <button type="button" class="segment-button" data-overview-metric="current_ma" data-report-only="power">电流</button>
              <button type="button" class="segment-button" data-overview-metric="cpu_pct">CPU</button>
              <button type="button" class="segment-button" data-overview-metric="frame_rate_fps" data-report-only="performance">FPS</button>
              <button type="button" class="segment-button" data-overview-metric="frame_time_ms" data-report-only="performance">帧耗时</button>
              <button type="button" class="segment-button" data-overview-metric="voltage_mv" data-report-only="power">电压</button>
              @@GPU_METRIC_BUTTON@@
            </div>
          </div>
          <div class="chart-surface">
            <svg id="overview-chart" role="img" aria-label="Selected power telemetry timeline"></svg>
            <div class="sample-control">
              <input id="overview-slider" type="range" min="0" max="@@SLIDER_MAX@@" value="0" aria-label="Selected telemetry sample">
              <div class="sample-detail" id="sample-detail" aria-live="polite"></div>
            </div>
          </div>
        </section>
        <section class="analysis-section" data-report-only="power"><h2>分析结论</h2><div class="finding-list">@@FINDINGS@@</div></section>
        <div class="split-layout" data-report-only="performance">
          <section class="analysis-section"><h2>性能上下文</h2><div class="data-table-wrap"><table><thead><tr><th>项目</th><th>当前值</th><th>说明</th></tr></thead><tbody>@@PERFORMANCE_CONTEXT_ROWS@@</tbody></table></div></section>
          <section class="analysis-section"><h2>帧表现结论</h2><div class="finding-list">@@FINDINGS@@</div></section>
        </div>
      </section>

      <section class="app-view" data-panel="timeline" hidden>
        <div class="view-heading"><div><h1>对齐时间线</h1><p>@@TIMELINE_COPY@@</p></div></div>
        <section class="analysis-section">
          <div class="chart-surface"><svg id="timeline-chart" role="img" aria-label="Aligned telemetry lanes"></svg></div>
        </section>
      </section>

      <section class="app-view" data-panel="flow" data-report-only="power" hidden>
        <div class="view-heading"><div><h1>测试流程</h1><p>前台应用与导入测试事件均已对齐至电池侧实测功率。</p></div><span class="source-tag counter">按设备 uptime 对齐</span></div>
        <section class="analysis-section">
          <div class="chart-surface"><svg id="flow-chart" role="img" aria-label="Power, foreground applications and external events on one timeline"></svg></div>
        </section>
        <section class="analysis-section"><h2>阶段能耗</h2><div class="data-table-wrap"><table><thead><tr><th>阶段 / 状态</th><th>次数</th><th>持续时间</th><th>能量</th><th>平均功率</th><th>置信度</th></tr></thead><tbody>@@PHASE_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>五分钟窗口</h2><div class="data-table-wrap"><table><thead><tr><th>窗口</th><th>有效覆盖</th><th>能量</th><th>平均功率</th><th>主要前台应用</th></tr></thead><tbody>@@WINDOW_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>导入的持续事件</h2><div class="data-table-wrap"><table><thead><tr><th>开始时间</th><th>事件</th><th>持续时间</th><th>能量</th><th>置信度</th></tr></thead><tbody>@@EVENT_ROWS@@</tbody></table></div></section>
      </section>

      <section class="app-view" data-panel="test-items" hidden>
        <div class="view-heading"><div><h1>测试项分析</h1><p>@@TEST_ITEM_COPY@@</p></div><span class="source-tag counter">点击表格行可聚焦时间窗口</span></div>
        @@CAPTURE_START_NOTE@@
        <section class="analysis-section">@@TEST_ITEM_STATUS@@</section>
        <section class="analysis-section">
          <div class="chart-toolbar">
            <div><h2>多泳道时间线</h2><p class="section-copy">@@TEST_ITEM_TIMELINE_COPY@@</p></div>
            <button type="button" class="segment-button" id="test-range-reset">显示全程</button>
          </div>
          <div class="chart-surface"><svg id="test-item-chart" role="img" aria-label="Per-test power, foreground activity, system activity, thermal and scheduler lanes"></svg></div>
        </section>
        <section class="analysis-section" data-report-only="power"><h2>功耗测试项矩阵</h2><div class="data-table-wrap"><table><thead><tr><th>测试项</th><th>时长</th><th>能量</th><th>mWh/min</th><th>平均 / P95 / 峰值功率</th><th>CPU 平均 / 峰值</th><th>GPU 平均 / 峰值</th><th>电池起止 / 传感器峰值</th><th>GC</th><th>kworker</th><th>平台后台活动 / 热限制</th><th>主要进程 / 活动</th><th>系统干扰</th><th>置信度</th></tr></thead><tbody>@@TEST_ITEM_ROWS@@</tbody></table></div></section>
        <section class="analysis-section" data-report-only="performance"><h2>性能测试项矩阵</h2><div class="data-table-wrap"><table><thead><tr>@@PERFORMANCE_TEST_ITEM_HEADERS@@</tr></thead><tbody>@@PERFORMANCE_TEST_ITEM_ROWS@@</tbody></table></div></section>
        <section class="analysis-section" data-report-only="power"><h2>单次执行明细</h2><div class="data-table-wrap"><table><thead><tr><th>开始时间</th><th>测试项</th><th>时长</th><th>能量</th><th>平均 / P95 / 峰值功率</th><th>系统干扰</th><th>前台应用</th></tr></thead><tbody>@@TEST_ITEM_SPAN_ROWS@@</tbody></table></div></section>
        <section class="analysis-section" data-report-only="performance"><h2>单次性能执行明细</h2><div class="data-table-wrap"><table><thead><tr><th>开始时间</th><th>测试项</th><th>时长</th><th>平均 FPS</th><th>1% Low</th><th>P95 / P99</th><th>异常帧</th><th>整机功耗</th><th>置信度</th></tr></thead><tbody>@@PERFORMANCE_TEST_ITEM_SPAN_ROWS@@</tbody></table></div></section>
        <div class="availability-note"><strong>解读边界</strong><span>@@TEST_ITEM_BOUNDARY@@</span></div>
      </section>

      <section class="app-view" data-panel="pressure" data-report-only="power" hidden>
        <div class="view-heading"><div><h1>功耗压力分析</h1><p>解释电流和功率为什么随任务负载、调度、频率与系统设置变化。</p></div><span class="source-tag counter">时间相关性，不是独立电源轨</span></div>
        @@POWER_PRESSURE_SECTIONS@@
      </section>

      <section class="app-view" data-panel="pipeline" data-report-only="performance" hidden>
        <div class="view-heading"><div><h1>渲染链路与帧延迟</h1><p>从 VSync 起步、UI/RenderThread、GPU 等待到 BufferQueue / SurfaceFlinger 合成，定位慢帧形成阶段。</p></div><span class="source-tag counter">Android framestats / 线程快照</span></div>
        <section class="analysis-section"><h2>阶段耗时分布</h2><div class="data-table-wrap"><table><thead><tr><th>阶段</th><th>帧数</th><th>平均</th><th>P95</th><th>P99</th><th>峰值</th></tr></thead><tbody>@@RENDER_PIPELINE_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>慢帧明细</h2><div class="data-table-wrap"><table><thead><tr><th>帧 ID</th><th>总耗时</th><th>超截止时间</th><th>最大阶段</th><th>阶段耗时</th></tr></thead><tbody>@@SLOW_FRAME_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>渲染与合成热点线程</h2><div class="data-table-wrap"><table><thead><tr><th>线程</th><th>PID / TID</th><th>可见时平均 CPU</th><th>峰值 CPU</th><th>快照数</th></tr></thead><tbody>@@RENDER_THREAD_ROWS@@</tbody></table></div></section>
        @@PERFORMANCE_POWER_RECORDING@@
      </section>

      <section class="app-view" data-panel="applications" data-report-only="power" hidden>
        <div class="view-heading"><div><h1>前台应用</h1><p>按采样到的前台包名分配电池侧整机实测能量。</p></div><span class="source-tag counter">上下文覆盖率 @@APP_COVERAGE@@%</span></div>
        <section class="analysis-section"><h2>应用能耗</h2><div class="data-table-wrap"><table><thead><tr><th>包名</th><th>持续时间</th><th>时间占比</th><th>能量</th><th>平均功率</th><th>进入次数</th><th>置信度</th></tr></thead><tbody>@@APPLICATION_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>前台应用切换</h2><div class="data-table-wrap"><table><thead><tr><th>已运行时间</th><th>包名</th><th>Activity</th></tr></thead><tbody>@@TRANSITION_ROWS@@</tbody></table></div></section>
      </section>

      <section class="app-view" data-panel="cpu" hidden>
        <div class="view-heading"><div><h1>@@CPU_TITLE@@</h1><p>@@CPU_COPY@@</p></div><span class="source-tag @@CPU_TAG_KIND@@">@@CPU_TAG@@</span></div>
        <section class="analysis-section">
          <div class="chart-toolbar"><div><h2>集群时间线</h2><p class="section-copy">@@CPU_TIMELINE_COPY@@</p></div><div class="segment-control" aria-label="CPU 集群">@@CPU_SELECTORS@@</div></div>
          <div class="chart-surface"><svg id="cpu-chart" role="img" aria-label="CPU cluster frequency impact timeline"></svg></div>
        </section>
        <section class="analysis-section"><div class="chart-toolbar"><div><h2>频率驻留</h2><p class="section-copy">按负载加权的低、中、高频使用比例。</p></div><div class="legend-row"><span><i class="legend-swatch band-low"></i>低频</span><span><i class="legend-swatch band-balanced"></i>中频</span><span><i class="legend-swatch band-high"></i>高频</span></div></div><div class="residency-list">@@RESIDENCY_ROWS@@</div></section>
        <section class="analysis-section" data-report-only="power"><h2>集群功耗压力汇总</h2><div class="data-table-wrap"><table><thead><tr><th>CPU 集群</th><th>负载</th><th>负载加权频率</th><th>观测峰值 / 硬件上限</th><th>模型功率</th><th>高频增量</th><th>功率相关性</th></tr></thead><tbody>@@CPU_ROWS@@</tbody></table></div></section>
        <section class="analysis-section" data-report-only="performance"><h2>CPU 调度资源汇总</h2><div class="data-table-wrap"><table><thead><tr><th>CPU 集群</th><th>平均负载</th><th>负载加权频率</th><th>观测峰值 / 硬件上限</th></tr></thead><tbody>@@PERFORMANCE_CPU_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>进程 CPU 快照</h2><div class="data-table-wrap"><table><thead><tr><th>进程</th><th>总占用</th><th>用户态</th><th>内核态</th></tr></thead><tbody>@@PROCESS_ROWS@@</tbody></table></div></section>
      </section>

      <section class="app-view" data-panel="system" data-report-only="performance" hidden>
        <div class="view-heading"><div><h1>性能资源与调度上下文</h1><p>帧表现结合 CPU/GPU、可用频率、cpuset、ADPF、热点进程和热限制解释；不进行进程功耗归因。</p></div><span class="source-tag counter">平台实测计数器</span></div>
        <div class="metric-grid">@@PERFORMANCE_CARDS@@</div>
        <div class="split-layout">
          <section class="analysis-section"><h2>刷新档位驻留</h2><div class="residency-list">@@REFRESH_RESIDENCY_ROWS@@</div></section>
          <section class="analysis-section"><h2>关键上下文</h2><div class="data-table-wrap"><table><thead><tr><th>项目</th><th>当前值</th><th>说明</th></tr></thead><tbody>@@PERFORMANCE_CONTEXT_ROWS@@</tbody></table></div></section>
        </div>
        <section class="analysis-section">@@PERFORMANCE_INTERFERENCE_STATUS@@</section>
        <section class="analysis-section"><h2>进程调度热点</h2><div class="data-table-wrap"><table><thead><tr><th>进程</th><th>全程平均 CPU</th><th>进入 Top 时平均</th><th>峰值</th><th>快照数</th></tr></thead><tbody>@@PERFORMANCE_PROCESS_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>RenderThread / SurfaceFlinger / Composer</h2><div class="data-table-wrap"><table><thead><tr><th>线程</th><th>PID / TID</th><th>可见时平均 CPU</th><th>峰值 CPU</th><th>快照数</th></tr></thead><tbody>@@RENDER_THREAD_ROWS@@</tbody></table></div></section>
        <div class="availability-note"><strong>解读边界</strong><span>帧率与帧耗时来自平台公开的合成器或前台窗口统计；详细 Android framestats 用于定位 UI / RenderThread 到 BufferQueue 的阶段耗时。插帧仅在读取到显式厂商开关时判定。整机功耗仅作为同期记录，不拆分到进程、UID 或组件。</span></div>
      </section>

      <section class="app-view" data-panel="gpu" hidden>
        <div class="view-heading"><div><h1>GPU 证据</h1><p>@@GPU_COPY@@</p></div></div>
        <section class="analysis-section"><div class="status-line">@@GPU_STATUS@@</div></section>
        <div>@@GPU_METRIC@@</div>
        <section class="analysis-section"><div class="chart-surface" id="gpu-chart-surface"><svg id="gpu-chart" role="img" aria-label="GPU telemetry timeline"></svg></div></section>
        <section class="analysis-section" data-report-only="power"><h2>按 UID 的 GPU 工作</h2><div class="data-table-wrap"><table><thead><tr><th>包名 / UID</th><th>活跃时长</th><th>运行占比</th><th>来源</th></tr></thead><tbody>@@GPU_UID_ROWS@@</tbody></table></div></section>
        <section class="analysis-section" data-report-only="power"><h2>GPU 进程内存快照</h2><div class="data-table-wrap"><table><thead><tr><th>进程</th><th>PID</th><th>内存</th><th>占 GPU 总量</th></tr></thead><tbody>@@GPU_MEMORY_ROWS@@</tbody></table></div></section>
        <div data-report-only="performance">@@PERFORMANCE_POWER_RECORDING@@</div>
      </section>

      <section class="app-view" data-panel="attribution" data-report-only="power" hidden>
        <div class="view-heading"><div><h1>功耗归因</h1><p>@@ATTRIBUTION_COPY@@</p></div><span class="source-tag @@ATTRIBUTION_TAG_KIND@@">@@ATTRIBUTION_TAG@@</span></div>
        @@ATTRIBUTION_NOTE@@
        <section class="analysis-section"><h2>模型贡献项</h2><div class="contributor-list">@@COMPONENT_ROWS@@</div></section>
        <section class="analysis-section"><h2>主要归因 UID</h2><div class="data-table-wrap"><table><thead><tr><th>包名</th><th>UID</th><th>模型用量</th><th>主要组件</th></tr></thead><tbody>@@UID_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>内核 Wakelock</h2><div class="data-table-wrap"><table><thead><tr><th>名称</th><th>持续时间</th><th>次数</th></tr></thead><tbody>@@WAKELOCK_ROWS@@</tbody></table></div></section>
      </section>

      <section class="app-view" data-panel="data" hidden>
        <div class="view-heading"><div><h1>数据质量</h1><p>查看本次测试的实测、计数器与模型数据来源。</p></div></div>
        <section class="analysis-section"><h2>采集项与干扰控制</h2><div class="data-table-wrap"><table><thead><tr><th>项目</th><th>状态</th><th>干扰等级</th><th>原因 / 恢复状态</th></tr></thead><tbody>@@CAPTURE_CONFIGURATION_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>数据来源</h2><div class="data-table-wrap"><table><thead><tr><th>指标</th><th>来源</th><th>类型</th></tr></thead><tbody>@@SOURCE_ROWS@@</tbody></table></div></section>
        @@WARNING_SECTION@@
        <section class="analysis-section"><details><summary>查看完整会话元数据</summary><pre class="metadata-block">@@METADATA@@</pre></details></section>
      </section>
    </main>
  </div>
</div>
<script>
(() => {
  const root = document.getElementById("mobile-profiler");
  const bundle = @@DATA@@;
  const samples = bundle.samples || [];
  const contexts = (bundle.contexts || []).slice().sort((a, b) => Number(a.uptime_s) - Number(b.uptime_s));
  const events = (bundle.events || []).slice().sort((a, b) => Number(a.device_uptime_s) - Number(b.device_uptime_s));
  const analysis = bundle.analysis || {};
  const cpu = analysis.cpu || { clusters: [], timeline: [] };
  const gpu = analysis.gpu || {};
  const testMode = root.dataset.testMode || "power";
  const frameTimeline = (((analysis.performance || {}).frame_rate_timeline) || []).slice().sort((a, b) => Number(a.uptime_s) - Number(b.uptime_s));
  const testItems = analysis.test_items || { rows: [], spans: [], instant_events: [] };
  const colors = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)"];
  let selectedIndex = 0;
  let overviewMetric = testMode === "performance" ? "frame_rate_fps" : "power_mw";
  let selectedCluster = cpu.clusters.length ? cpu.clusters[0].name : null;
  let testRange = null;

  const metricDefinitions = {
    power_mw: { label: "功率", unit: "mW", color: colors[0], value: sample => sample.power_mw },
    current_ma: { label: "放电电流幅值", unit: "mA", color: colors[1], value: sample => sample.current_ma },
    cpu_pct: { label: "CPU", unit: "%", color: colors[2], value: sample => sample.cpu_pct },
    frame_rate_fps: { label: "帧率", unit: "FPS", color: colors[0], value: sample => (frameForUptime(sample.uptime_s) || {}).frame_rate_fps },
    frame_time_ms: { label: "帧耗时 P99", unit: "ms", color: colors[3], value: sample => (frameForUptime(sample.uptime_s) || {}).frame_time_p99_ms || (frameForUptime(sample.uptime_s) || {}).frame_time_p95_ms },
    voltage_mv: { label: "电压", unit: "mV", color: colors[4], value: sample => sample.voltage_mv },
    gpu_frequency_mhz: { label: "GPU 频率", unit: "MHz", color: colors[5], value: sample => sample.gpu_frequency_mhz },
    gpu_load_pct: { label: "GPU 负载", unit: "%", color: colors[5], value: sample => sample.gpu_load_pct }
  };

  function svgNode(name, attrs = {}, text = "") {
    const node = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (text) node.textContent = text;
    return node;
  }
  function finite(value) { return value != null && Number.isFinite(Number(value)); }
  function clusterLabel(cluster) {
    const labels = { Little: "小核", Big: "大核", Performance: "性能核", Prime: "超大核" };
    return labels[cluster.label] || cluster.label || cluster.name || "CPU";
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
  function chartWidth(svg) {
    return Math.max(360, Math.round(svg.getBoundingClientRect().width || 1080));
  }
  function maxTime() { return Math.max(1, ...samples.map(sample => Number(sample.elapsed_s || 0))); }
  function sessionStartUptime() { return samples.length ? Number(samples[0].uptime_s || 0) : 0; }
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
    let minimum = Math.min(...valid);
    let maximum = Math.max(...valid);
    if (minimum === maximum) { minimum -= 1; maximum += 1; }
    const pad = (maximum - minimum) * 0.08;
    return [minimum - pad, maximum + pad];
  }
  function pointString(values, x, y) {
    return values.map((value, index) => finite(value) ? `${x(samples[index].elapsed_s).toFixed(2)},${y(Number(value)).toFixed(2)}` : null).filter(Boolean).join(" ");
  }
  function attachOverlay(svg, width, height, left, right, top, bottom, rangeStart = 0, rangeEnd = maxTime()) {
    const overlay = svgNode("rect", { x: left, y: top, width: width - left - right, height: height - top - bottom, fill: "transparent" });
    overlay.addEventListener("mousemove", event => {
      const rect = svg.getBoundingClientRect();
      const localX = (event.clientX - rect.left) / rect.width * width;
      const time = Math.max(rangeStart, Math.min(rangeEnd, rangeStart + (localX - left) / (width - left - right) * (rangeEnd - rangeStart)));
      selectSample(nearestIndex(time));
    });
    svg.appendChild(overlay);
  }

  function renderOverview() {
    const svg = root.querySelector("#overview-chart");
    if (!svg || !samples.length) return;
    const width = chartWidth(svg), height = 300;
    const margin = { left: width < 560 ? 62 : 72, right: width < 560 ? 14 : 24, top: 22, bottom: 38 };
    const metric = metricDefinitions[overviewMetric];
    const values = samples.map(metric.value);
    const [minimum, maximum] = domain(values);
    const x = time => margin.left + Number(time) / maxTime() * (width - margin.left - margin.right);
    const y = value => margin.top + (maximum - value) / (maximum - minimum) * (height - margin.top - margin.bottom);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.replaceChildren();
    for (let tick = 0; tick <= 4; tick++) {
      const ratio = tick / 4;
      const yPos = margin.top + ratio * (height - margin.top - margin.bottom);
      const value = maximum - ratio * (maximum - minimum);
      svg.appendChild(svgNode("line", { x1: margin.left, x2: width - margin.right, y1: yPos, y2: yPos, class: "grid" }));
      svg.appendChild(svgNode("text", { x: margin.left - 9, y: yPos + 4, "text-anchor": "end", class: "axis-text" }, format(value, metric.unit)));
    }
    for (let tick = 0; tick <= 5; tick++) {
      const seconds = maxTime() * tick / 5;
      const xPos = x(seconds);
      svg.appendChild(svgNode("line", { x1: xPos, x2: xPos, y1: margin.top, y2: height - margin.bottom, class: "grid" }));
      svg.appendChild(svgNode("text", { x: xPos, y: height - 12, "text-anchor": "middle", class: "axis-text" }, formatTime(seconds)));
    }
    svg.appendChild(svgNode("polyline", { points: pointString(values, x, y), fill: "none", stroke: metric.color, "stroke-width": 2.2 }));
    const selected = samples[selectedIndex];
    const selectedValue = metric.value(selected);
    const xPos = x(selected.elapsed_s);
    svg.appendChild(svgNode("line", { x1: xPos, x2: xPos, y1: margin.top, y2: height - margin.bottom, class: "crosshair" }));
    if (finite(selectedValue)) svg.appendChild(svgNode("circle", { cx: xPos, cy: y(Number(selectedValue)), r: 4.5, class: "selected-point", stroke: metric.color }));
    attachOverlay(svg, width, height, margin.left, margin.right, margin.top, margin.bottom);
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
      svg.appendChild(svgNode("polyline", { points: pointString(values, x, y), fill: "none", stroke: lane.color, "stroke-width": 1.8 }));
      const selectedValue = values[selectedIndex];
      if (finite(selectedValue)) svg.appendChild(svgNode("circle", { cx: x(samples[selectedIndex].elapsed_s), cy: y(Number(selectedValue)), r: 3.5, class: "selected-point", stroke: lane.color }));
    });
    const selectedX = x(samples[selectedIndex].elapsed_s);
    svg.appendChild(svgNode("line", { x1: selectedX, x2: selectedX, y1: top, y2: height - bottom, class: "crosshair" }));
    attachOverlay(svg, width, height, left, right, top, bottom);
  }

  function timelineLanes() {
    const lanes = testMode === "performance"
      ? [
          { label: "帧率", unit: "FPS", color: colors[0], value: sample => (frameForUptime(sample.uptime_s) || {}).frame_rate_fps },
          { label: "P99 帧耗时", unit: "ms", color: colors[3], value: sample => (frameForUptime(sample.uptime_s) || {}).frame_time_p99_ms || (frameForUptime(sample.uptime_s) || {}).frame_time_p95_ms },
          { label: "CPU 总负载", unit: "%", color: colors[2], value: sample => sample.cpu_pct }
        ]
      : [
          { label: "功率", unit: "mW", color: colors[0], value: sample => sample.power_mw },
          { label: "电流", unit: "mA", color: colors[1], value: sample => sample.current_ma },
          { label: "CPU 总负载", unit: "%", color: colors[2], value: sample => sample.cpu_pct }
        ];
    cpu.clusters.forEach((cluster, index) => lanes.push({ label: `${clusterLabel(cluster)}频率`, unit: "MHz", color: colors[(index + 3) % colors.length], value: sample => (sample.frequencies_mhz || {})[cluster.name] }));
    if (gpu.frequency_available) lanes.push({ label: "GPU 频率", unit: "MHz", color: colors[5], value: sample => sample.gpu_frequency_mhz });
    if (gpu.load_available) lanes.push({ label: "GPU 负载", unit: "%", color: colors[1], value: sample => sample.gpu_load_pct });
    if (samples.some(sample => finite(sample.memory_frequency_mhz))) lanes.push({ label: "内存频率", unit: "MHz", color: colors[4], value: sample => sample.memory_frequency_mhz });
    if (testMode === "performance") lanes.push({ label: "整机功耗记录", unit: "mW", color: colors[5], value: sample => sample.power_mw });
    return lanes;
  }
  function renderTimeline() { renderLanes(root.querySelector("#timeline-chart"), timelineLanes()); }

  function renderFlow() {
    if (testMode !== "power") return;
    const svg = root.querySelector("#flow-chart");
    if (!svg || !samples.length) return;
    const width = chartWidth(svg), height = 320;
    const left = width < 620 ? 88 : 124, right = 20, top = 24, powerBottom = 202, bandTop = 232, bandHeight = 32, bottom = 38;
    const plotWidth = width - left - right;
    const x = time => left + Math.max(0, Math.min(maxTime(), Number(time))) / maxTime() * plotWidth;
    const powers = samples.map(sample => sample.power_mw);
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
    svg.appendChild(svgNode("text", { x: 12, y: top + 18, class: "lane-label" }, "功率"));
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

    svg.appendChild(svgNode("polyline", { points: pointString(powers, x, y), fill: "none", stroke: colors[0], "stroke-width": 2 }));

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
    const primaryValues = visibleSamples.map(sample => testMode === "performance" ? (frameForUptime(sample.uptime_s) || {}).frame_rate_fps : sample.power_mw);
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
    svg.appendChild(svgNode("text", { x: 12, y: powerTop + 20, class: "lane-label" }, testMode === "performance" ? "帧率" : "整机功率"));
    const selected = samples[selectedIndex];
    const selectedPrimary = testMode === "performance" ? (frameForUptime(selected && selected.uptime_s) || {}).frame_rate_fps : selected && selected.power_mw;
    svg.appendChild(svgNode("text", { x: 12, y: powerTop + 41, class: "lane-value" }, format(selectedPrimary, testMode === "performance" ? "FPS" : "mW")));
    laneNames.forEach((name, index) => {
      const top = laneStart + index * laneHeight;
      svg.appendChild(svgNode("text", { x: 12, y: top + 25, class: "lane-label" }, name));
      svg.appendChild(svgNode("line", { x1: left, x2: width - right, y1: top + laneHeight - 2, y2: top + laneHeight - 2, class: "grid" }));
    });
    const powerPoints = visibleSamples
      .map(sample => ({ sample, value: testMode === "performance" ? (frameForUptime(sample.uptime_s) || {}).frame_rate_fps : sample.power_mw }))
      .filter(item => finite(item.value))
      .map(item => `${x(item.sample.elapsed_s).toFixed(2)},${y(Number(item.value)).toFixed(2)}`)
      .join(" ");
    svg.appendChild(svgNode("polyline", { points: powerPoints, fill: "none", stroke: colors[0], "stroke-width": 2 }));

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
    const timeline = cpu.timeline || [];
    const lanes = [
      { label: `${clusterLabel(cluster)}频率`, unit: "MHz", color: colors[0], value: sample => (sample.frequencies_mhz || {})[cluster.name] },
      { label: `${clusterLabel(cluster)}负载`, unit: "%", color: colors[1], value: sample => (sample.cluster_cpu_pct || {})[cluster.name] }
    ];
    if (testMode === "power") {
      lanes.push(
        { label: "CPU 模型功率", unit: "mW", color: colors[2], value: (sample, index) => (((timeline[index] || {}).clusters || {})[cluster.name] || {}).modeled_power_mw },
        { label: "高频增量", unit: "mW", color: colors[3], value: (sample, index) => (((timeline[index] || {}).clusters || {})[cluster.name] || {}).frequency_premium_mw }
      );
    }
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
    const frame = frameForUptime(sample.uptime_s) || {};
    detail.innerHTML = testMode === "performance"
      ? `<span><strong>${formatTime(sample.elapsed_s)}</strong></span><span>帧率 <strong>${format(frame.frame_rate_fps, "FPS")}</strong></span><span>P99 <strong>${format(frame.frame_time_p99_ms || frame.frame_time_p95_ms, "ms")}</strong></span><span>CPU <strong>${format(sample.cpu_pct, "%")}</strong></span><span>整机功耗 <strong>${format(sample.power_mw, "mW")}</strong></span>`
      : `<span><strong>${formatTime(sample.elapsed_s)}</strong></span><span>功率 <strong>${format(sample.power_mw, "mW")}</strong></span><span>电流 <strong>${format(sample.current_ma, "mA")}</strong></span><span>CPU <strong>${format(sample.cpu_pct, "%")}</strong></span><span>应用 <strong>${packageName}</strong></span>`;
  }
  function selectSample(index) {
    selectedIndex = Math.max(0, Math.min(samples.length - 1, Number(index)));
    updateSampleDetail();
    renderOverview();
    renderTimeline();
    renderFlow();
    renderTestItems();
    renderCpu();
    renderGpu();
  }

  root.querySelectorAll(".nav-tab").forEach(tab => tab.addEventListener("click", () => {
    const view = tab.dataset.view;
    root.querySelectorAll(".nav-tab").forEach(peer => peer.setAttribute("aria-selected", peer === tab ? "true" : "false"));
    root.querySelectorAll(".app-view").forEach(panel => { panel.hidden = panel.dataset.panel !== view; });
    window.requestAnimationFrame(() => {
      if (view === "overview") renderOverview();
      if (view === "timeline") renderTimeline();
      if (view === "flow") renderFlow();
      if (view === "test-items") renderTestItems();
      if (view === "cpu") renderCpu();
      if (view === "gpu") renderGpu();
    });
  }));
  root.querySelectorAll("[data-overview-metric]").forEach(button => button.addEventListener("click", () => {
    overviewMetric = button.dataset.overviewMetric;
    root.querySelectorAll("[data-overview-metric]").forEach(peer => peer.classList.toggle("active", peer === button));
    renderOverview();
  }));
  root.querySelectorAll("[data-overview-metric]").forEach(button => {
    button.classList.toggle("active", button.dataset.overviewMetric === overviewMetric);
  });
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
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => selectSample(selectedIndex), 100);
  });
  selectSample(testMode === "performance" ? Math.max(0, samples.length - 1) : 0);
  const initialView = new URLSearchParams(window.location.search).get("view") || window.location.hash.slice(1);
  const initialTab = initialView ? Array.from(root.querySelectorAll("[data-view]")).find(tab => tab.dataset.view === initialView && window.getComputedStyle(tab).display !== "none") : null;
  if (initialTab && initialView !== "overview") initialTab.click();
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
                "采集器进程单独标记。"
            ),
            "system_source": "DVT sysmontap 快照",
            "system_activity_title": "系统与采集器活动聚合",
            "priority_activity_title": "重点系统 / 采集器活动",
            "thermal_copy": (
                "展示 iOS 当前可观测的电池温度；未公开的热严重度、冷却设备、"
                "cpuset 与调度策略会明确留空。"
            ),
            "gpu_copy": (
                "使用 DVT Graphics 的 Device / Renderer / Tiler 利用率作为相对活动证据；"
                "它不是 GPU 电源轨，也不是应用级电能归因。"
            ),
            "attribution_copy": (
                "iOS 整机物理功率与 DVT 进程相对功耗分数分开展示；"
                "当前不把相对分数换算成 mW 或应用独占能量。"
            ),
            "attribution_tag_kind": "context",
            "attribution_tag": "相对分数 ≠ mW",
            "attribution_note": (
                '<div class="availability-note"><strong>iOS 归因边界</strong>'
                '<span>DVT powerScore 仅用于同一会话内的相对诊断。整机功率仍来自电池侧 '
                'PowerTelemetry；两类数值不可相加，也不构成单进程电源轨测量。</span></div>'
            ),
            "test_item_copy": (
                "按前台应用或导入测试阶段计算整机能量，并同步检查可见的进程、"
                "采集器、GPU 与电池温度证据。"
            ),
            "test_item_timeline_copy": (
                "功率、前台应用、测试项、系统活动和平台可提供的热 / 调度证据共享同一时间轴。"
            ),
            "test_item_boundary": (
                "测试项能量来自低频刷新的电池侧整机物理功率；进程 CPU、DVT 相对功耗分数、"
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
                "功率、前台 Ability、测试项、系统活动与 HarmonyOS 热 / 电源上下文共享设备实时时钟。"
            ),
            "test_item_boundary": (
                "测试项能量来自 BatteryService 电流与电压的整机实测；进程 CPU、系统活动、频率与温度只表示同期证据。"
                "重叠测试项不可相加，也不能当作单应用或单硬件电源轨功耗。"
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
        result.update(
            {
                "report_subtitle": "游戏帧表现、渲染链路与资源调度分析",
                "overview_title": "性能测试概览",
                "overview_tag": "帧计数器 + 整机功耗记录",
                "overview_copy": (
                    "帧表现使用前台窗口 / 合成器计数器；CPU、GPU、调度与整机功耗统一使用设备 uptime 对齐。"
                ),
                "timeline_copy": (
                    "帧率窗口、CPU/GPU、可用资源、调度与整机功耗记录位于同一设备时间轴。"
                ),
                "cpu_title": "CPU 调度与频率上下文",
                "cpu_copy": (
                    "展示各集群负载、频率驻留与可用核心分配，用于判断主线程 / RenderThread 是否受到调度竞争或频率上限影响。"
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
                    "整机功耗仅记录测试窗口均值，不拆分到进程、UID、Wakelock 或硬件组件。"
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
    profile = _report_mode_profile(_report_platform_profile(metadata, device), metadata)
    platform = profile["platform"]
    analysis_mode = analysis.get("test_mode") if isinstance(analysis, dict) else None
    test_mode = str(analysis_mode or metadata.get("test_mode") or "power").strip().lower()
    if test_mode not in {"power", "performance"}:
        test_mode = "power"
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
    warnings = analysis.get("warnings", []) if isinstance(analysis, dict) else []
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
        "@@PERFORMANCE_POWER_RECORDING@@": _performance_power_recording(analysis),
        "@@POWER_PRESSURE_SECTIONS@@": _power_pressure_sections(analysis),
        "@@POWER_PRESSURE_DRIVER_ROWS@@": _power_pressure_driver_rows(analysis),
        "@@POWER_PRESSURE_TASK_ROWS@@": _power_pressure_task_rows(analysis),
        "@@RUNTIME_SETTING_ROWS@@": _runtime_setting_rows(analysis),
        "@@MEMORY_PRESSURE_SUMMARY@@": _memory_pressure_summary(analysis),
        "@@RENDER_PIPELINE_ROWS@@": _render_pipeline_rows(analysis),
        "@@SLOW_FRAME_ROWS@@": _slow_frame_rows(analysis),
        "@@RENDER_THREAD_ROWS@@": _render_thread_rows(analysis),
        "@@PERFORMANCE_PROCESS_ROWS@@": _performance_process_rows(analysis),
        "@@PERFORMANCE_INTERFERENCE_STATUS@@": _performance_interference_status(analysis),
        "@@GPU_METRIC_BUTTON@@": gpu_button,
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
        "@@SOURCE_ROWS@@": _source_rows(analysis),
        "@@CAPTURE_CONFIGURATION_ROWS@@": _capture_configuration_rows(metadata),
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
