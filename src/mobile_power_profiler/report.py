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
            "放电电流正幅值",
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
    for item in system.get("top_processes", [])[:20] if isinstance(system, dict) else []:
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
        f"检测到 {overlap} 组重叠测试区间，重叠行的能量不能相加。"
        if overlap
        else "测试项之间未检测到重叠区间。"
    )
    return (
        '<div class="priority-callout"><span class="status-dot good"></span><div>'
        f'<strong>已按{_escape(test_items.get("source_label"))}生成 {int(test_items.get("row_count") or 0)} 个测试项</strong>'
        f'<span>{_escape(overlap_note)} GC、kworker、DEX/更新与热限制均为时间重叠证据，不是进程独占功耗。</span>'
        "</div></div>"
    )


def _test_item_rows(analysis: Dict[str, object]) -> str:
    test_items = analysis.get("test_items", {})
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
                f'Thermal {int(item.get("maximum_thermal_status") or 0)}</span>'
            )
        else:
            temperature_text = (
                f'传感器峰值 {_number(item.get("maximum_temperature_c"))} °C'
                f'<span class="cell-sub">Thermal {int(item.get("maximum_thermal_status") or 0)}</span>'
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
            f'<td>DEX/更新 {_number(item.get("dex_update_overlap_s"))} s<span class="cell-sub">热限制 {_number(item.get("thermal_throttling_overlap_s"))} s</span></td>'
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
    return "".join(rows) or '<tr><td colspan="4" class="empty-cell">cpuset CPU 范围不可用。</td></tr>'


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
    return "".join(rows) or '<tr><td colspan="5" class="empty-cell">未观察到 ADPF 会话。</td></tr>'


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
    return "".join(rows) or '<tr><td colspan="5" class="empty-cell">ActivityManager 进程状态历史不可用。</td></tr>'


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
        else:
            fallback = "UID 活跃时长和内存快照" if memory.get("available") else "UID 活跃时长"
            status = (
                f'<span class="status-dot warning"></span><span>{_escape(model)} 实时节点受限</span>'
                f'<strong>已使用 {_escape(fallback)}作为回退证据</strong>'
            )
        metric = (
            '<div class="availability-note"><strong>GPU 频率/负载数据源不可用</strong>'
            f'<span>{_escape(gpu.get("unavailable_reason") or ("DVT Graphics 未返回可用事件。" if platform == "ios" else "ADB shell 无法读取 OEM GPU devfreq 节点；这通常是量产系统权限限制。"))}</span></div>'
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
  #mobile-power-profiler {
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
  #mobile-power-profiler * { box-sizing: border-box; }
  #mobile-power-profiler button, #mobile-power-profiler input { font: inherit; }
  #mobile-power-profiler button { letter-spacing: 0; }
  #mobile-power-profiler .app-topbar {
    min-height: 58px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 10px 18px;
    border-bottom: 1px solid var(--app-border);
    background: #151719;
  }
  #mobile-power-profiler .brand-block, #mobile-power-profiler .session-block,
  #mobile-power-profiler .device-block, #mobile-power-profiler .metric-top,
  #mobile-power-profiler .view-heading, #mobile-power-profiler .chart-toolbar,
  #mobile-power-profiler .status-line, #mobile-power-profiler .legend-row {
    display: flex;
    align-items: center;
  }
  #mobile-power-profiler .brand-block { gap: 10px; min-width: 190px; }
  #mobile-power-profiler .brand-mark {
    width: 26px;
    height: 26px;
    border: 1px solid var(--series-1);
    border-radius: 5px;
    display: grid;
    place-items: center;
    color: var(--series-1);
    font-weight: 500;
  }
  #mobile-power-profiler .brand-block strong { display: block; font-size: 15px; font-weight: 500; }
  #mobile-power-profiler .brand-block span, #mobile-power-profiler .session-block span,
  #mobile-power-profiler .device-block span { color: var(--app-muted); font-size: 12px; }
  #mobile-power-profiler .session-block { gap: 9px; min-width: 0; }
  #mobile-power-profiler .session-block div { min-width: 0; }
  #mobile-power-profiler .session-block strong, #mobile-power-profiler .device-block strong {
    display: block;
    font-size: 13px;
    font-weight: 500;
    overflow-wrap: anywhere;
  }
  #mobile-power-profiler .device-block { gap: 10px; justify-content: flex-end; text-align: right; }
  #mobile-power-profiler .status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--series-1); flex: 0 0 auto; }
  #mobile-power-profiler .status-dot.good { background: var(--series-2); }
  #mobile-power-profiler .status-dot.warning { background: var(--series-3); }
  #mobile-power-profiler .app-workspace { display: grid; grid-template-columns: 178px minmax(0, 1fr); }
  #mobile-power-profiler .side-tabs {
    border-right: 1px solid var(--app-border);
    padding: 14px 10px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    background: #151719;
  }
  #mobile-power-profiler .nav-tab {
    border: 0;
    background: transparent;
    color: var(--app-muted);
    text-align: left;
    padding: 9px 10px;
    border-radius: 5px;
    cursor: pointer;
  }
  #mobile-power-profiler .nav-tab:hover { background: var(--app-surface-2); color: var(--app-text); }
  #mobile-power-profiler .nav-tab[aria-selected="true"] { background: var(--app-surface-2); color: var(--app-text); box-shadow: inset 2px 0 0 var(--series-1); }
  #mobile-power-profiler .app-content { min-width: 0; padding: 22px; }
  #mobile-power-profiler .app-view[hidden] { display: none; }
  #mobile-power-profiler .app-view { display: grid; gap: 22px; min-width: 0; }
  #mobile-power-profiler .view-heading { justify-content: space-between; gap: 16px; align-items: flex-end; flex-wrap: wrap; }
  #mobile-power-profiler h1, #mobile-power-profiler h2, #mobile-power-profiler h3,
  #mobile-power-profiler p { margin: 0; letter-spacing: 0; }
  #mobile-power-profiler h1 { font-size: 22px; font-weight: 500; }
  #mobile-power-profiler h2 { font-size: 16px; font-weight: 500; }
  #mobile-power-profiler h3 { font-size: 14px; font-weight: 500; }
  #mobile-power-profiler .view-heading p, #mobile-power-profiler .section-copy,
  #mobile-power-profiler .metric-context, #mobile-power-profiler .cell-sub,
  #mobile-power-profiler .finding-row p, #mobile-power-profiler .availability-note span {
    color: var(--app-muted);
    font-size: 12px;
  }
  #mobile-power-profiler .metric-grid { display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 10px; }
  #mobile-power-profiler .metric-card {
    background: var(--app-surface);
    border: 1px solid var(--app-border);
    border-radius: 6px;
    padding: 13px 14px;
    min-width: 0;
  }
  #mobile-power-profiler .metric-card.compact { max-width: 260px; }
  #mobile-power-profiler .metric-top { justify-content: space-between; gap: 8px; color: var(--app-muted); font-size: 12px; }
  #mobile-power-profiler .metric-value { margin-top: 12px; font-size: 25px; font-weight: 500; white-space: nowrap; }
  #mobile-power-profiler .metric-value small { color: var(--app-muted); font-size: 12px; font-weight: 400; }
  #mobile-power-profiler .metric-context { margin-top: 5px; overflow-wrap: anywhere; }
  #mobile-power-profiler .source-tag {
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
  #mobile-power-profiler .source-tag.measured { color: var(--series-2); border-color: color-mix(in srgb, var(--series-2) 55%, var(--app-border)); }
  #mobile-power-profiler .source-tag.counter, #mobile-power-profiler .source-tag.driver { color: var(--series-1); border-color: color-mix(in srgb, var(--series-1) 55%, var(--app-border)); }
  #mobile-power-profiler .source-tag.model { color: var(--series-3); border-color: color-mix(in srgb, var(--series-3) 55%, var(--app-border)); }
  #mobile-power-profiler .source-tag.medium { color: var(--series-1); border-color: color-mix(in srgb, var(--series-1) 55%, var(--app-border)); }
  #mobile-power-profiler .source-tag.low { color: var(--series-4); border-color: color-mix(in srgb, var(--series-4) 55%, var(--app-border)); }
  #mobile-power-profiler .source-tag.high { color: var(--series-4); border-color: color-mix(in srgb, var(--series-4) 70%, var(--app-border)); background: rgba(228, 111, 111, .08); }
  #mobile-power-profiler .analysis-section { min-width: 0; border-top: 1px solid var(--app-border); padding-top: 16px; }
  #mobile-power-profiler .chart-toolbar { justify-content: space-between; gap: 14px; flex-wrap: wrap; margin-bottom: 10px; }
  #mobile-power-profiler .segment-control { display: inline-flex; border: 1px solid var(--app-border); border-radius: 5px; overflow: hidden; }
  #mobile-power-profiler .segment-button {
    border: 0;
    border-right: 1px solid var(--app-border);
    color: var(--app-muted);
    background: transparent;
    padding: 6px 10px;
    cursor: pointer;
  }
  #mobile-power-profiler .segment-button:last-child { border-right: 0; }
  #mobile-power-profiler .segment-button:hover { color: var(--app-text); background: var(--app-surface-2); }
  #mobile-power-profiler .segment-button.active { background: var(--app-text); color: var(--app-bg); }
  #mobile-power-profiler .chart-surface {
    background: var(--app-surface);
    border: 1px solid var(--app-border);
    border-radius: 6px;
    min-width: 0;
    overflow: hidden;
  }
  #mobile-power-profiler .chart-surface svg { display: block; width: 100%; height: auto; min-height: 260px; }
  #mobile-power-profiler .chart-surface .grid { stroke: var(--app-border); stroke-width: 1; }
  #mobile-power-profiler .chart-surface .axis-text, #mobile-power-profiler .chart-surface .lane-label { fill: var(--app-muted); font-size: 11px; }
  #mobile-power-profiler .chart-surface .lane-value { fill: var(--app-text); font-size: 11px; }
  #mobile-power-profiler .chart-surface .crosshair { stroke: var(--app-muted); stroke-width: 1; }
  #mobile-power-profiler .chart-surface .selected-point { fill: var(--app-bg); stroke-width: 2; }
  #mobile-power-profiler .chart-surface .event-span { fill: var(--series-3); opacity: .12; }
  #mobile-power-profiler .chart-surface .event-line { stroke: var(--series-3); stroke-width: 1; }
  #mobile-power-profiler .chart-surface .app-band { opacity: .78; }
  #mobile-power-profiler .chart-surface .test-band { fill: var(--series-1); opacity: .28; }
  #mobile-power-profiler .chart-surface .activity-band { opacity: .72; }
  #mobile-power-profiler .chart-surface .thermal-band { fill: var(--series-4); opacity: .2; }
  #mobile-power-profiler .chart-surface .scheduler-band { fill: var(--series-6); opacity: .38; }
  #mobile-power-profiler .chart-surface .focus-window { fill: var(--series-1); opacity: .06; stroke: var(--series-1); stroke-width: 1; }
  #mobile-power-profiler .chart-surface .band-label { fill: var(--app-text); font-size: 11px; }
  #mobile-power-profiler .sample-control { padding: 10px 12px 12px; border-top: 1px solid var(--app-border); }
  #mobile-power-profiler .sample-control input { width: 100%; accent-color: var(--series-1); }
  #mobile-power-profiler .sample-detail { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; color: var(--app-muted); font-size: 12px; }
  #mobile-power-profiler .sample-detail strong { color: var(--app-text); font-weight: 500; }
  #mobile-power-profiler .split-layout { display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(280px, .8fr); gap: 22px; }
  #mobile-power-profiler .data-table-wrap { overflow-x: auto; max-width: 100%; }
  #mobile-power-profiler table { width: 100%; border-collapse: collapse; min-width: 620px; }
  #mobile-power-profiler th, #mobile-power-profiler td { border-bottom: 1px solid var(--app-border); padding: 9px 8px; text-align: left; vertical-align: middle; font-size: 12px; }
  #mobile-power-profiler th { color: var(--app-muted); font-weight: 400; }
  #mobile-power-profiler td strong { display: block; font-weight: 500; }
  #mobile-power-profiler .test-item-row { cursor: pointer; }
  #mobile-power-profiler .test-item-row:hover td { background: rgba(79, 195, 215, .055); }
  #mobile-power-profiler .cell-sub { display: block; margin-top: 2px; }
  #mobile-power-profiler .empty-cell { color: var(--app-muted); }
  #mobile-power-profiler .finding-list { display: grid; gap: 0; }
  #mobile-power-profiler .finding-row { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--app-border); }
  #mobile-power-profiler .finding-row strong { font-size: 13px; font-weight: 500; }
  #mobile-power-profiler .finding-row p { margin-top: 3px; overflow-wrap: anywhere; }
  #mobile-power-profiler .residency-list { display: grid; gap: 15px; }
  #mobile-power-profiler .residency-row { display: grid; grid-template-columns: 150px minmax(180px, 1fr) 150px; gap: 12px; align-items: center; }
  #mobile-power-profiler .residency-row > div:first-child { display: grid; }
  #mobile-power-profiler .residency-row > div:first-child span { color: var(--app-muted); font-size: 11px; }
  #mobile-power-profiler .stacked-bar { height: 10px; display: flex; overflow: hidden; background: var(--app-surface-2); }
  #mobile-power-profiler .band-low { background: var(--series-2); }
  #mobile-power-profiler .band-balanced { background: var(--series-1); }
  #mobile-power-profiler .band-high { background: var(--series-3); }
  #mobile-power-profiler .residency-values { display: flex; justify-content: flex-end; gap: 10px; color: var(--app-muted); font-size: 11px; }
  #mobile-power-profiler .status-line { gap: 9px; flex-wrap: wrap; }
  #mobile-power-profiler .status-line strong { margin-left: auto; font-size: 12px; font-weight: 500; }
  #mobile-power-profiler .availability-note { border-left: 3px solid var(--series-3); padding: 8px 11px; display: grid; gap: 3px; background: var(--app-surface); }
  #mobile-power-profiler .availability-note strong { font-size: 13px; font-weight: 500; }
  #mobile-power-profiler .priority-callout { display: flex; align-items: center; gap: 11px; padding: 13px 15px; border: 1px solid var(--app-border); border-left: 3px solid var(--series-2); background: var(--app-surface); }
  #mobile-power-profiler .priority-callout.active { border-left-color: var(--series-3); background: rgba(240, 161, 94, .07); }
  #mobile-power-profiler .priority-callout div { display: grid; gap: 3px; }
  #mobile-power-profiler .priority-callout strong { font-size: 13px; font-weight: 550; }
  #mobile-power-profiler .priority-callout span { color: var(--app-muted); font-size: 11px; }
  #mobile-power-profiler .contributor-list { display: grid; gap: 12px; }
  #mobile-power-profiler .contributor-row { display: grid; grid-template-columns: minmax(130px, .8fr) minmax(180px, 2fr) 70px; gap: 12px; align-items: center; }
  #mobile-power-profiler .contributor-row > div:first-child { display: grid; }
  #mobile-power-profiler .contributor-row span { color: var(--app-muted); font-size: 11px; }
  #mobile-power-profiler .bar-track { height: 8px; background: var(--app-surface-2); overflow: hidden; }
  #mobile-power-profiler .bar-track > span { display: block; height: 100%; background: var(--series-3); }
  #mobile-power-profiler .contributor-value { text-align: right; font-size: 12px; }
  #mobile-power-profiler .warning-list { margin: 0; padding-left: 18px; color: var(--series-3); font-size: 12px; }
  #mobile-power-profiler .metadata-block { margin: 0; padding: 13px; background: var(--app-surface); border: 1px solid var(--app-border); border-radius: 6px; color: var(--app-muted); white-space: pre-wrap; overflow-wrap: anywhere; font-size: 11px; }
  #mobile-power-profiler .legend-row { gap: 14px; flex-wrap: wrap; color: var(--app-muted); font-size: 11px; }
  #mobile-power-profiler .legend-row span { display: inline-flex; align-items: center; gap: 5px; }
  #mobile-power-profiler .legend-swatch { width: 9px; height: 9px; display: inline-block; }
  @media (max-width: 980px) {
    #mobile-power-profiler .metric-grid { grid-template-columns: repeat(2, minmax(150px, 1fr)); }
    #mobile-power-profiler .split-layout { grid-template-columns: 1fr; }
    #mobile-power-profiler .residency-row { grid-template-columns: 130px minmax(160px, 1fr); }
    #mobile-power-profiler .residency-values { grid-column: 2; justify-content: flex-start; }
  }
  @media (max-width: 720px) {
    #mobile-power-profiler .app-topbar { align-items: flex-start; flex-wrap: wrap; }
    #mobile-power-profiler .session-block { order: 3; width: 100%; }
    #mobile-power-profiler .app-workspace { grid-template-columns: 1fr; }
    #mobile-power-profiler .side-tabs { border-right: 0; border-bottom: 1px solid var(--app-border); flex-direction: row; overflow-x: auto; padding: 8px 10px; }
    #mobile-power-profiler .nav-tab { flex: 0 0 auto; text-align: center; }
    #mobile-power-profiler .nav-tab[aria-selected="true"] { box-shadow: inset 0 -2px 0 var(--series-1); }
    #mobile-power-profiler .app-content { padding: 16px 12px; }
    #mobile-power-profiler .metric-grid { grid-template-columns: 1fr 1fr; }
    #mobile-power-profiler .residency-row { grid-template-columns: 1fr; }
    #mobile-power-profiler .residency-values { grid-column: 1; }
    #mobile-power-profiler .contributor-row { grid-template-columns: minmax(0, 1fr) 65px; }
    #mobile-power-profiler .contributor-row .bar-track { grid-column: 1 / -1; grid-row: 2; }
  }
  @media (max-width: 440px) {
    #mobile-power-profiler .metric-grid { grid-template-columns: 1fr; }
    #mobile-power-profiler .device-block { width: 100%; justify-content: flex-start; text-align: left; }
    #mobile-power-profiler .metric-value { font-size: 22px; }
    #mobile-power-profiler .sample-detail { display: grid; grid-template-columns: 1fr; }
  }
</style>
<div id="mobile-power-profiler">
  <header class="app-topbar">
    <div class="brand-block">
      <span class="brand-mark">P</span>
      <div><strong>PowerScope Mobile</strong><span>移动设备电池与系统资源分析</span></div>
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
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="flow">测试流程</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="test-items">测试项分析</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="applications">应用</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="cpu">CPU</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="system">系统活动</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="thermal">热控 / 调度</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="gpu">GPU</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="attribution">功耗归因</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="data">数据质量</button>
    </nav>
    <main class="app-content">
      <section class="app-view" data-panel="overview">
        <div class="view-heading"><div><h1>测试概览</h1><p>@@GENERATED@@</p></div><span class="source-tag measured">电量计整机实测</span></div>
        <div class="metric-grid">@@SUMMARY_CARDS@@</div>
        <section class="analysis-section">
          <div class="chart-toolbar">
            <div><h2>实测遥测</h2><p class="section-copy">电流、电压与资源计数器统一使用设备 uptime 时间轴。</p></div>
            <div class="segment-control" aria-label="概览指标">
              <button type="button" class="segment-button active" data-overview-metric="power_mw">功率</button>
              <button type="button" class="segment-button" data-overview-metric="current_ma">电流</button>
              <button type="button" class="segment-button" data-overview-metric="cpu_pct">CPU</button>
              <button type="button" class="segment-button" data-overview-metric="voltage_mv">电压</button>
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
        <div class="split-layout">
          <section class="analysis-section"><h2>资源汇总</h2><div class="data-table-wrap"><table><thead><tr><th>CPU 集群</th><th>负载</th><th>负载加权频率</th><th>观测峰值 / 硬件上限</th><th>模型功率</th><th>高频增量</th><th>功率相关性</th></tr></thead><tbody>@@CPU_ROWS@@</tbody></table></div></section>
          <section class="analysis-section"><h2>分析结论</h2><div class="finding-list">@@FINDINGS@@</div></section>
        </div>
      </section>

      <section class="app-view" data-panel="timeline" hidden>
        <div class="view-heading"><div><h1>对齐时间线</h1><p>整机实测、CPU 集群与 GPU 证据位于同一时间轴。</p></div></div>
        <section class="analysis-section">
          <div class="chart-surface"><svg id="timeline-chart" role="img" aria-label="Aligned telemetry lanes"></svg></div>
        </section>
      </section>

      <section class="app-view" data-panel="flow" hidden>
        <div class="view-heading"><div><h1>测试流程</h1><p>前台应用与导入测试事件均已对齐至电池侧实测功率。</p></div><span class="source-tag counter">按设备 uptime 对齐</span></div>
        <section class="analysis-section">
          <div class="chart-surface"><svg id="flow-chart" role="img" aria-label="Power, foreground applications and external events on one timeline"></svg></div>
        </section>
        <section class="analysis-section"><h2>阶段能耗</h2><div class="data-table-wrap"><table><thead><tr><th>阶段 / 状态</th><th>次数</th><th>持续时间</th><th>能量</th><th>平均功率</th><th>置信度</th></tr></thead><tbody>@@PHASE_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>五分钟窗口</h2><div class="data-table-wrap"><table><thead><tr><th>窗口</th><th>有效覆盖</th><th>能量</th><th>平均功率</th><th>主要前台应用</th></tr></thead><tbody>@@WINDOW_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>导入的持续事件</h2><div class="data-table-wrap"><table><thead><tr><th>开始时间</th><th>事件</th><th>持续时间</th><th>能量</th><th>置信度</th></tr></thead><tbody>@@EVENT_ROWS@@</tbody></table></div></section>
      </section>

      <section class="app-view" data-panel="test-items" hidden>
        <div class="view-heading"><div><h1>一小时测试项分析</h1><p>@@TEST_ITEM_COPY@@</p></div><span class="source-tag counter">点击表格行可聚焦时间窗口</span></div>
        @@CAPTURE_START_NOTE@@
        <section class="analysis-section">@@TEST_ITEM_STATUS@@</section>
        <section class="analysis-section">
          <div class="chart-toolbar">
            <div><h2>多泳道时间线</h2><p class="section-copy">@@TEST_ITEM_TIMELINE_COPY@@</p></div>
            <button type="button" class="segment-button" id="test-range-reset">显示全程</button>
          </div>
          <div class="chart-surface"><svg id="test-item-chart" role="img" aria-label="Per-test power, foreground activity, system activity, thermal and scheduler lanes"></svg></div>
        </section>
        <section class="analysis-section"><h2>测试项矩阵</h2><div class="data-table-wrap"><table><thead><tr><th>测试项</th><th>时长</th><th>能量</th><th>mWh/min</th><th>平均 / P95 / 峰值功率</th><th>CPU 平均 / 峰值</th><th>GPU 平均 / 峰值</th><th>电池起止 / 传感器峰值</th><th>GC</th><th>kworker</th><th>DEX / 热限制</th><th>主要进程 / 活动</th><th>系统干扰</th><th>置信度</th></tr></thead><tbody>@@TEST_ITEM_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>单次执行明细</h2><div class="data-table-wrap"><table><thead><tr><th>开始时间</th><th>测试项</th><th>时长</th><th>能量</th><th>平均 / P95 / 峰值功率</th><th>系统干扰</th><th>前台应用</th></tr></thead><tbody>@@TEST_ITEM_SPAN_ROWS@@</tbody></table></div></section>
        <div class="availability-note"><strong>解读边界</strong><span>@@TEST_ITEM_BOUNDARY@@</span></div>
      </section>

      <section class="app-view" data-panel="applications" hidden>
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
        <section class="analysis-section"><h2>集群汇总</h2><div class="data-table-wrap"><table><thead><tr><th>CPU 集群</th><th>负载</th><th>负载加权频率</th><th>观测峰值 / 硬件上限</th><th>模型功率</th><th>高频增量</th><th>功率相关性</th></tr></thead><tbody>@@CPU_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>进程 CPU 快照</h2><div class="data-table-wrap"><table><thead><tr><th>进程</th><th>总占用</th><th>用户态</th><th>内核态</th></tr></thead><tbody>@@PROCESS_ROWS@@</tbody></table></div></section>
      </section>

      <section class="app-view" data-panel="system" hidden>
        <div class="view-heading"><div><h1>全系统活动</h1><p>@@SYSTEM_COPY@@</p></div><span class="source-tag counter">@@SYSTEM_SOURCE@@</span></div>
        <section class="analysis-section">@@PRIORITY_STATUS@@</section>
        <section class="analysis-section"><h2>@@SYSTEM_ACTIVITY_TITLE@@</h2><div class="data-table-wrap"><table><thead><tr><th>活动类型</th><th>检测点 / 窗口</th><th>估算持续时间</th><th>CPU 平均 / 峰值</th><th>同期功率 / 相对基线</th><th>功率相关性</th><th>平均 / 最高温度</th><th>线程 / 进程证据</th><th>置信度</th></tr></thead><tbody>@@ACTIVITY_GROUP_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>@@PRIORITY_ACTIVITY_TITLE@@</h2><div class="data-table-wrap"><table><thead><tr><th>活动</th><th>检测点 / 窗口</th><th>估算持续时间</th><th>CPU 平均 / 峰值</th><th>活动期间功率</th><th>相对基线</th><th>关联增量能量</th><th>置信度</th></tr></thead><tbody>@@PRIORITY_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>全程热点进程</h2><div class="data-table-wrap"><table><thead><tr><th>进程</th><th>全程平均 CPU</th><th>进入 Top 时平均</th><th>峰值</th><th>可见时功率 / 相对基线</th><th>功率相关性</th><th>相对功耗分数 平均 / 峰值</th><th>快照数</th></tr></thead><tbody>@@SYSTEM_PROCESS_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>热点线程</h2><div class="data-table-wrap"><table><thead><tr><th>线程 / 进程</th><th>PID / TID</th><th>可见时平均</th><th>峰值</th><th>快照数</th></tr></thead><tbody>@@SYSTEM_THREAD_ROWS@@</tbody></table></div></section>
      </section>

      <section class="app-view" data-panel="thermal" hidden>
        <div class="view-heading"><div><h1>热控与调度状态</h1><p>@@THERMAL_COPY@@</p></div><span class="source-tag counter">系统可观测状态</span></div>
        <section class="analysis-section"><h2>热传感器</h2><div class="data-table-wrap"><table><thead><tr><th>传感器</th><th>最低</th><th>平均</th><th>最高</th><th>最高级别</th><th>首个热阈值</th><th>阈值余量</th><th>功率相关性</th></tr></thead><tbody>@@THERMAL_SENSOR_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>冷却设备</h2><div class="data-table-wrap"><table><thead><tr><th>设备</th><th>最大值</th><th>激活快照数</th></tr></thead><tbody>@@COOLING_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>cpuset 边界</h2><div class="data-table-wrap"><table><thead><tr><th>分组</th><th>最新 CPU 范围</th><th>观测值</th><th>是否变化</th></tr></thead><tbody>@@CPUSET_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>CPU Policy</h2><div class="data-table-wrap"><table><thead><tr><th>Policy</th><th>CPU</th><th>Governor</th><th>最低频率</th><th>最高频率</th><th>core_ctl</th><th>可见性</th></tr></thead><tbody>@@SCHEDULER_POLICY_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>ADPF Performance Hint 会话</h2><div class="data-table-wrap"><table><thead><tr><th>PID / UID</th><th>TID</th><th>目标时长</th><th>标志</th><th>快照数</th></tr></thead><tbody>@@HINT_SESSION_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>ActivityManager 进程状态</h2><div class="data-table-wrap"><table><thead><tr><th>进程</th><th>PID / UID</th><th>Proc state / adj</th><th>Sched group</th><th>冻结状态</th></tr></thead><tbody>@@SCHEDULER_PROCESS_ROWS@@</tbody></table></div></section>
        <div class="availability-note"><strong>可观测边界</strong><span>量产系统通常限制 sched_debug、运行时 Governor/uclamp 控制和完整 OEM 热控算法。本页只报告系统实际暴露的状态和阈值，不推断未公开的厂商策略。</span></div>
      </section>

      <section class="app-view" data-panel="gpu" hidden>
        <div class="view-heading"><div><h1>GPU 证据</h1><p>@@GPU_COPY@@</p></div></div>
        <section class="analysis-section"><div class="status-line">@@GPU_STATUS@@</div></section>
        <div>@@GPU_METRIC@@</div>
        <section class="analysis-section"><div class="chart-surface" id="gpu-chart-surface"><svg id="gpu-chart" role="img" aria-label="GPU telemetry timeline"></svg></div></section>
        <section class="analysis-section"><h2>按 UID 的 GPU 工作</h2><div class="data-table-wrap"><table><thead><tr><th>包名 / UID</th><th>活跃时长</th><th>运行占比</th><th>来源</th></tr></thead><tbody>@@GPU_UID_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>GPU 进程内存快照</h2><div class="data-table-wrap"><table><thead><tr><th>进程</th><th>PID</th><th>内存</th><th>占 GPU 总量</th></tr></thead><tbody>@@GPU_MEMORY_ROWS@@</tbody></table></div></section>
      </section>

      <section class="app-view" data-panel="attribution" hidden>
        <div class="view-heading"><div><h1>功耗归因</h1><p>@@ATTRIBUTION_COPY@@</p></div><span class="source-tag @@ATTRIBUTION_TAG_KIND@@">@@ATTRIBUTION_TAG@@</span></div>
        @@ATTRIBUTION_NOTE@@
        <section class="analysis-section"><h2>模型贡献项</h2><div class="contributor-list">@@COMPONENT_ROWS@@</div></section>
        <section class="analysis-section"><h2>主要归因 UID</h2><div class="data-table-wrap"><table><thead><tr><th>包名</th><th>UID</th><th>模型用量</th><th>主要组件</th></tr></thead><tbody>@@UID_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>内核 Wakelock</h2><div class="data-table-wrap"><table><thead><tr><th>名称</th><th>持续时间</th><th>次数</th></tr></thead><tbody>@@WAKELOCK_ROWS@@</tbody></table></div></section>
      </section>

      <section class="app-view" data-panel="data" hidden>
        <div class="view-heading"><div><h1>数据质量</h1><p>查看本次测试的实测、计数器与模型数据来源。</p></div></div>
        <section class="analysis-section"><h2>数据来源</h2><div class="data-table-wrap"><table><thead><tr><th>指标</th><th>来源</th><th>类型</th></tr></thead><tbody>@@SOURCE_ROWS@@</tbody></table></div></section>
        @@WARNING_SECTION@@
        <section class="analysis-section"><h2>会话元数据</h2><pre class="metadata-block">@@METADATA@@</pre></section>
      </section>
    </main>
  </div>
</div>
<script>
(() => {
  const root = document.getElementById("mobile-power-profiler");
  const bundle = @@DATA@@;
  const samples = bundle.samples || [];
  const contexts = (bundle.contexts || []).slice().sort((a, b) => Number(a.uptime_s) - Number(b.uptime_s));
  const events = (bundle.events || []).slice().sort((a, b) => Number(a.device_uptime_s) - Number(b.device_uptime_s));
  const analysis = bundle.analysis || {};
  const cpu = analysis.cpu || { clusters: [], timeline: [] };
  const gpu = analysis.gpu || {};
  const testItems = analysis.test_items || { rows: [], spans: [], instant_events: [] };
  const colors = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)"];
  let selectedIndex = 0;
  let overviewMetric = "power_mw";
  let selectedCluster = cpu.clusters.length ? cpu.clusters[0].name : null;
  let testRange = null;

  const metricDefinitions = {
    power_mw: { label: "功率", unit: "mW", color: colors[0], value: sample => sample.power_mw },
    current_ma: { label: "放电电流幅值", unit: "mA", color: colors[1], value: sample => sample.current_ma },
    cpu_pct: { label: "CPU", unit: "%", color: colors[2], value: sample => sample.cpu_pct },
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
    const lanes = [
      { label: "功率", unit: "mW", color: colors[0], value: sample => sample.power_mw },
      { label: "电流", unit: "mA", color: colors[1], value: sample => sample.current_ma },
      { label: "CPU 总负载", unit: "%", color: colors[2], value: sample => sample.cpu_pct }
    ];
    cpu.clusters.forEach((cluster, index) => lanes.push({ label: `${clusterLabel(cluster)}频率`, unit: "MHz", color: colors[(index + 3) % colors.length], value: sample => (sample.frequencies_mhz || {})[cluster.name] }));
    if (gpu.frequency_available) lanes.push({ label: "GPU 频率", unit: "MHz", color: colors[5], value: sample => sample.gpu_frequency_mhz });
    if (gpu.load_available) lanes.push({ label: "GPU 负载", unit: "%", color: colors[1], value: sample => sample.gpu_load_pct });
    return lanes;
  }
  function renderTimeline() { renderLanes(root.querySelector("#timeline-chart"), timelineLanes()); }

  function renderFlow() {
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
    const laneNames = ["前台应用", "测试项 / 阶段", "系统活动", "Thermal", "ADPF / 调度"];
    const height = laneStart + laneHeight * laneNames.length + bottom;
    const rangeStart = testRange ? Math.max(0, Number(testRange[0])) : 0;
    const rangeEnd = testRange ? Math.min(maxTime(), Number(testRange[1])) : maxTime();
    const safeEnd = Math.max(rangeStart + 1, rangeEnd);
    const plotWidth = width - left - right;
    const x = time => left + (Math.max(rangeStart, Math.min(safeEnd, Number(time))) - rangeStart) / (safeEnd - rangeStart) * plotWidth;
    const visibleSamples = samples.filter(sample => Number(sample.elapsed_s) >= rangeStart && Number(sample.elapsed_s) <= safeEnd);
    const powerValues = visibleSamples.map(sample => sample.power_mw);
    const [minimum, maximum] = domain(powerValues);
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
    svg.appendChild(svgNode("text", { x: 12, y: powerTop + 20, class: "lane-label" }, "整机功率"));
    const selected = samples[selectedIndex];
    svg.appendChild(svgNode("text", { x: 12, y: powerTop + 41, class: "lane-value" }, format(selected && selected.power_mw, "mW")));
    laneNames.forEach((name, index) => {
      const top = laneStart + index * laneHeight;
      svg.appendChild(svgNode("text", { x: 12, y: top + 25, class: "lane-label" }, name));
      svg.appendChild(svgNode("line", { x1: left, x2: width - right, y1: top + laneHeight - 2, y2: top + laneHeight - 2, class: "grid" }));
    });
    const powerPoints = visibleSamples
      .filter(sample => finite(sample.power_mw))
      .map(sample => `${x(sample.elapsed_s).toFixed(2)},${y(Number(sample.power_mw)).toFixed(2)}`)
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
    renderLanes(svg, [
      { label: `${clusterLabel(cluster)}频率`, unit: "MHz", color: colors[0], value: sample => (sample.frequencies_mhz || {})[cluster.name] },
      { label: `${clusterLabel(cluster)}负载`, unit: "%", color: colors[1], value: sample => (sample.cluster_cpu_pct || {})[cluster.name] },
      { label: "CPU 模型功率", unit: "mW", color: colors[2], value: (sample, index) => (((timeline[index] || {}).clusters || {})[cluster.name] || {}).modeled_power_mw },
      { label: "高频增量", unit: "mW", color: colors[3], value: (sample, index) => (((timeline[index] || {}).clusters || {})[cluster.name] || {}).frequency_premium_mw }
    ]);
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
    detail.innerHTML = `<span><strong>${formatTime(sample.elapsed_s)}</strong></span><span>功率 <strong>${format(sample.power_mw, "mW")}</strong></span><span>电流 <strong>${format(sample.current_ma, "mA")}</strong></span><span>CPU <strong>${format(sample.cpu_pct, "%")}</strong></span><span>应用 <strong>${packageName}</strong></span>`;
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
  selectSample(0);
  const initialView = new URLSearchParams(window.location.search).get("view") || window.location.hash.slice(1);
  const initialTab = initialView ? Array.from(root.querySelectorAll("[data-view]")).find(tab => tab.dataset.view === initialView) : null;
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


def build_report_fragment(bundle: Dict[str, object]) -> str:
    bundle = _report_bundle(bundle)
    metadata = bundle.get("metadata", {})
    analysis = bundle.get("analysis", {})
    samples = bundle.get("samples", [])
    summary = analysis.get("summary", {}) if isinstance(analysis, dict) else {}
    device = metadata.get("device", {}) if isinstance(metadata, dict) else {}
    profile = _report_platform_profile(metadata, device)
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
        "@@GPU_METRIC_BUTTON@@": gpu_button,
        "@@SLIDER_MAX@@": str(max(0, len(samples) - 1)),
        "@@CPU_ROWS@@": cpu_rows,
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
        "@@APP_COVERAGE@@": _number(
            analysis.get("applications", {}).get("coverage_pct")
            if isinstance(analysis.get("applications"), dict)
            else None,
            1,
            "0",
        ),
        "@@SOURCE_ROWS@@": _source_rows(analysis),
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
