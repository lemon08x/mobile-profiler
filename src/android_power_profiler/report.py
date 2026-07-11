from __future__ import annotations

import copy
import html
import json
from pathlib import Path
from typing import Dict, List, Tuple

from .models import APP_NAME


def _escape(value: object) -> str:
    return html.escape(str(value))


def _number(value: object, digits: int = 1, fallback: str = "n/a") -> str:
    if not isinstance(value, (int, float)):
        return fallback
    return f"{float(value):.{digits}f}"


def _json_for_script(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")


def _summary_cards(summary: Dict[str, object]) -> str:
    cards = [
        (
            "Average power",
            f"{float(summary.get('average_power_mw') or 0.0) / 1000.0:.3f}",
            "W",
            f"P95 {float(summary.get('p95_power_mw') or 0.0) / 1000.0:.3f} W",
            "measured",
        ),
        (
            "Battery current",
            f"{float(summary.get('average_current_ma') or 0.0):.1f}",
            "mA",
            "positive discharge magnitude",
            "measured",
        ),
        (
            "Battery voltage",
            f"{float(summary.get('average_voltage_mv') or 0.0) / 1000.0:.3f}",
            "V",
            f"{float(summary.get('energy_per_minute_mwh') or 0.0):.2f} mWh/min",
            "measured",
        ),
        (
            "CPU utilization",
            f"{float(summary.get('average_cpu_pct') or 0.0):.1f}",
            "%",
            f"Peak {float(summary.get('maximum_cpu_pct') or 0.0):.1f}%",
            "counter",
        ),
    ]
    return "".join(
        '<article class="metric-card">'
        f'<div class="metric-top"><span>{_escape(label)}</span><span class="source-tag {kind}">{_escape(kind)}</span></div>'
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
        label = str(cluster.get("label", name))
        selector_buttons.append(
            f'<button type="button" class="segment-button{" active" if index == 0 else ""}" '
            f'data-cpu-cluster="{_escape(name)}">{_escape(label)}</button>'
        )
        cores = ", ".join(str(value) for value in cluster.get("cores", [])) or "n/a"
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
            f'<div><strong>{_escape(label)}</strong><span>load-weighted frequency residency</span></div>'
            '<div class="stacked-bar" role="img" '
            f'aria-label="{_escape(label)} low {low:.1f} percent, balanced {balanced:.1f} percent, high {high:.1f} percent">'
            f'<span class="band-low" style="width:{low:.3f}%"></span>'
            f'<span class="band-balanced" style="width:{balanced:.3f}%"></span>'
            f'<span class="band-high" style="width:{high:.3f}%"></span>'
            "</div>"
            f'<div class="residency-values"><span>L {low:.0f}%</span><span>M {balanced:.0f}%</span><span>H {high:.0f}%</span></div>'
            "</div>"
        )
    if not table_rows:
        table_rows.append('<tr><td colspan="7" class="empty-cell">CPU cluster data unavailable.</td></tr>')
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
    return "".join(rows) or '<tr><td colspan="4" class="empty-cell">Process CPU snapshot unavailable.</td></tr>'


def _gpu_content(analysis: Dict[str, object]) -> Tuple[str, str, str]:
    gpu = analysis.get("gpu", {})
    available = bool(gpu.get("frequency_available")) if isinstance(gpu, dict) else False
    if available:
        status = (
            '<span class="status-dot good"></span><span>GPU frequency captured</span>'
            f'<strong>{_number(gpu.get("average_frequency_mhz"), 0)} MHz avg</strong>'
        )
        metric = (
            '<article class="metric-card compact"><div class="metric-top"><span>GPU frequency</span>'
            '<span class="source-tag counter">counter</span></div>'
            f'<div class="metric-value">{_number(gpu.get("average_frequency_mhz"), 0)} <small>MHz</small></div>'
            f'<div class="metric-context">Peak {_number(gpu.get("maximum_frequency_mhz"), 0)} MHz</div></article>'
        )
    else:
        reason = gpu.get("unavailable_reason") if isinstance(gpu, dict) else None
        status = (
            '<span class="status-dot warning"></span><span>GPU frequency not exposed</span>'
            '<strong>UID activity fallback active</strong>'
        )
        metric = (
            '<div class="availability-note"><strong>Frequency source unavailable</strong>'
            f'<span>{_escape(reason or "No readable OEM GPU devfreq node was available to the ADB shell.")}</span></div>'
        )
    rows = []
    for item in (gpu.get("work_by_uid", []) if isinstance(gpu, dict) else [])[:15]:
        packages = item.get("packages") or [f"UID {item.get('uid')}"]
        rows.append(
            "<tr>"
            f'<td><strong>{_escape(", ".join(str(value) for value in packages[:2]))}</strong><span class="cell-sub">UID {_escape(item.get("uid"))}</span></td>'
            f'<td>{float(item.get("active_ms") or 0.0):.1f} ms</td>'
            f'<td>{_number(item.get("active_ratio_pct"), 2)}%</td>'
            '<td><span class="source-tag driver">driver</span></td>'
            "</tr>"
        )
    uid_rows = "".join(rows) or '<tr><td colspan="4" class="empty-cell">GPU work-duration data unavailable.</td></tr>'
    return status, metric, uid_rows


def _component_rows(analysis: Dict[str, object]) -> str:
    components = analysis.get("components", [])
    maximum = max((float(item.get("modeled_power_mw") or 0.0) for item in components), default=1.0)
    rows = []
    for item in components[:12]:
        value = float(item.get("modeled_power_mw") or 0.0)
        width = max(0.0, min(100.0, value / maximum * 100.0))
        rows.append(
            '<div class="contributor-row">'
            f'<div><strong>{_escape(item.get("name", "unknown"))}</strong><span>{_escape(item.get("source", "model"))}</span></div>'
            f'<div class="bar-track"><span style="width:{width:.3f}%"></span></div>'
            f'<div class="contributor-value">{value:.0f} mW</div>'
            "</div>"
        )
    return "".join(rows) or '<div class="availability-note">No component model was available.</div>'


def _uid_rows(analysis: Dict[str, object]) -> str:
    rows = []
    battery_usage = analysis.get("battery_usage", {})
    for item in battery_usage.get("uids", [])[:15] if isinstance(battery_usage, dict) else []:
        packages = item.get("packages") or [item.get("token", "unknown")]
        components = ", ".join(
            f"{key} {float(value):.3f}"
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
    return "".join(rows) or '<tr><td colspan="4" class="empty-cell">UID model data unavailable.</td></tr>'


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
    return "".join(rows) or '<tr><td colspan="3" class="empty-cell">No kernel wakelocks parsed.</td></tr>'


def _finding_rows(analysis: Dict[str, object]) -> str:
    return "".join(
        '<article class="finding-row">'
        f'<span class="source-tag {_escape(item.get("level", "context"))}">{_escape(item.get("level", "info"))}</span>'
        f'<div><strong>{_escape(item.get("title", "Finding"))}</strong><p>{_escape(item.get("detail", ""))}</p></div>'
        "</article>"
        for item in analysis.get("findings", [])
    )


def _source_rows(analysis: Dict[str, object]) -> str:
    return "".join(
        "<tr>"
        f'<td><strong>{_escape(item.get("metric"))}</strong></td>'
        f'<td>{_escape(item.get("source"))}</td>'
        f'<td><span class="source-tag {_escape(item.get("kind", "context"))}">{_escape(item.get("kind"))}</span></td>'
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
            f'<span class="cell-sub">{_escape(", ".join(item.get("activities", [])[:2]) or "no activity detail")}</span></td>'
            f'<td>{_number(item.get("duration_s"), 1)} s</td>'
            f'<td>{_number(item.get("time_pct"), 1)}%</td>'
            f'<td>{_number(item.get("energy_mwh"), 2)} mWh</td>'
            f'<td>{_number(item.get("average_power_mw"), 0)} mW</td>'
            f'<td>{int(item.get("transition_count") or 0)}</td>'
            f'<td><span class="source-tag {_escape(item.get("confidence", "context"))}">{_escape(item.get("confidence", "n/a"))}</span></td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="7" class="empty-cell">Foreground application context unavailable.</td></tr>'


def _transition_rows(analysis: Dict[str, object]) -> str:
    applications = analysis.get("applications", {})
    rows = []
    transitions = applications.get("transitions", []) if isinstance(applications, dict) else []
    for item in transitions[:100]:
        rows.append(
            "<tr>"
            f'<td>{_number(item.get("elapsed_s"), 1)} s</td>'
            f'<td><strong>{_escape(item.get("package"))}</strong></td>'
            f'<td>{_escape(item.get("activity") or "n/a")}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="3" class="empty-cell">No application transitions captured.</td></tr>'


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
            f'<td><span class="source-tag {_escape(item.get("confidence", "context"))}">{_escape(item.get("confidence", "n/a"))}</span></td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="6" class="empty-cell">Import a timestamped log to calculate phase energy.</td></tr>'


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
            f'<td><span class="source-tag {_escape(item.get("confidence", "context"))}">{_escape(item.get("confidence", "n/a"))}</span></td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="5" class="empty-cell">No duration events available.</td></tr>'


def _window_rows(analysis: Dict[str, object]) -> str:
    rows = []
    for item in analysis.get("long_windows", []):
        rows.append(
            "<tr>"
            f'<td>{_number(item.get("start_s"), 0)} - {_number(item.get("end_s"), 0)} s</td>'
            f'<td>{_number(item.get("covered_duration_s"), 1)} s</td>'
            f'<td>{_number(item.get("energy_mwh"), 2)} mWh</td>'
            f'<td>{_number(item.get("average_power_mw"), 0)} mW</td>'
            f'<td>{_escape(item.get("dominant_app") or "unknown")}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="5" class="empty-cell">No five-minute windows available.</td></tr>'


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
  #android-power-profiler {
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
  #android-power-profiler * { box-sizing: border-box; }
  #android-power-profiler button, #android-power-profiler input { font: inherit; }
  #android-power-profiler button { letter-spacing: 0; }
  #android-power-profiler .app-topbar {
    min-height: 58px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 10px 18px;
    border-bottom: 1px solid var(--app-border);
    background: #151719;
  }
  #android-power-profiler .brand-block, #android-power-profiler .session-block,
  #android-power-profiler .device-block, #android-power-profiler .metric-top,
  #android-power-profiler .view-heading, #android-power-profiler .chart-toolbar,
  #android-power-profiler .status-line, #android-power-profiler .legend-row {
    display: flex;
    align-items: center;
  }
  #android-power-profiler .brand-block { gap: 10px; min-width: 190px; }
  #android-power-profiler .brand-mark {
    width: 26px;
    height: 26px;
    border: 1px solid var(--series-1);
    border-radius: 5px;
    display: grid;
    place-items: center;
    color: var(--series-1);
    font-weight: 500;
  }
  #android-power-profiler .brand-block strong { display: block; font-size: 15px; font-weight: 500; }
  #android-power-profiler .brand-block span, #android-power-profiler .session-block span,
  #android-power-profiler .device-block span { color: var(--app-muted); font-size: 12px; }
  #android-power-profiler .session-block { gap: 9px; min-width: 0; }
  #android-power-profiler .session-block div { min-width: 0; }
  #android-power-profiler .session-block strong, #android-power-profiler .device-block strong {
    display: block;
    font-size: 13px;
    font-weight: 500;
    overflow-wrap: anywhere;
  }
  #android-power-profiler .device-block { gap: 10px; justify-content: flex-end; text-align: right; }
  #android-power-profiler .status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--series-1); flex: 0 0 auto; }
  #android-power-profiler .status-dot.good { background: var(--series-2); }
  #android-power-profiler .status-dot.warning { background: var(--series-3); }
  #android-power-profiler .app-workspace { display: grid; grid-template-columns: 178px minmax(0, 1fr); }
  #android-power-profiler .side-tabs {
    border-right: 1px solid var(--app-border);
    padding: 14px 10px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    background: #151719;
  }
  #android-power-profiler .nav-tab {
    border: 0;
    background: transparent;
    color: var(--app-muted);
    text-align: left;
    padding: 9px 10px;
    border-radius: 5px;
    cursor: pointer;
  }
  #android-power-profiler .nav-tab:hover { background: var(--app-surface-2); color: var(--app-text); }
  #android-power-profiler .nav-tab[aria-selected="true"] { background: var(--app-surface-2); color: var(--app-text); box-shadow: inset 2px 0 0 var(--series-1); }
  #android-power-profiler .app-content { min-width: 0; padding: 22px; }
  #android-power-profiler .app-view[hidden] { display: none; }
  #android-power-profiler .app-view { display: grid; gap: 22px; min-width: 0; }
  #android-power-profiler .view-heading { justify-content: space-between; gap: 16px; align-items: flex-end; flex-wrap: wrap; }
  #android-power-profiler h1, #android-power-profiler h2, #android-power-profiler h3,
  #android-power-profiler p { margin: 0; letter-spacing: 0; }
  #android-power-profiler h1 { font-size: 22px; font-weight: 500; }
  #android-power-profiler h2 { font-size: 16px; font-weight: 500; }
  #android-power-profiler h3 { font-size: 14px; font-weight: 500; }
  #android-power-profiler .view-heading p, #android-power-profiler .section-copy,
  #android-power-profiler .metric-context, #android-power-profiler .cell-sub,
  #android-power-profiler .finding-row p, #android-power-profiler .availability-note span {
    color: var(--app-muted);
    font-size: 12px;
  }
  #android-power-profiler .metric-grid { display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 10px; }
  #android-power-profiler .metric-card {
    background: var(--app-surface);
    border: 1px solid var(--app-border);
    border-radius: 6px;
    padding: 13px 14px;
    min-width: 0;
  }
  #android-power-profiler .metric-card.compact { max-width: 260px; }
  #android-power-profiler .metric-top { justify-content: space-between; gap: 8px; color: var(--app-muted); font-size: 12px; }
  #android-power-profiler .metric-value { margin-top: 12px; font-size: 25px; font-weight: 500; white-space: nowrap; }
  #android-power-profiler .metric-value small { color: var(--app-muted); font-size: 12px; font-weight: 400; }
  #android-power-profiler .metric-context { margin-top: 5px; overflow-wrap: anywhere; }
  #android-power-profiler .source-tag {
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
  #android-power-profiler .source-tag.measured { color: var(--series-2); border-color: color-mix(in srgb, var(--series-2) 55%, var(--app-border)); }
  #android-power-profiler .source-tag.counter, #android-power-profiler .source-tag.driver { color: var(--series-1); border-color: color-mix(in srgb, var(--series-1) 55%, var(--app-border)); }
  #android-power-profiler .source-tag.model { color: var(--series-3); border-color: color-mix(in srgb, var(--series-3) 55%, var(--app-border)); }
  #android-power-profiler .source-tag.medium { color: var(--series-1); border-color: color-mix(in srgb, var(--series-1) 55%, var(--app-border)); }
  #android-power-profiler .source-tag.low { color: var(--series-4); border-color: color-mix(in srgb, var(--series-4) 55%, var(--app-border)); }
  #android-power-profiler .analysis-section { min-width: 0; border-top: 1px solid var(--app-border); padding-top: 16px; }
  #android-power-profiler .chart-toolbar { justify-content: space-between; gap: 14px; flex-wrap: wrap; margin-bottom: 10px; }
  #android-power-profiler .segment-control { display: inline-flex; border: 1px solid var(--app-border); border-radius: 5px; overflow: hidden; }
  #android-power-profiler .segment-button {
    border: 0;
    border-right: 1px solid var(--app-border);
    color: var(--app-muted);
    background: transparent;
    padding: 6px 10px;
    cursor: pointer;
  }
  #android-power-profiler .segment-button:last-child { border-right: 0; }
  #android-power-profiler .segment-button:hover { color: var(--app-text); background: var(--app-surface-2); }
  #android-power-profiler .segment-button.active { background: var(--app-text); color: var(--app-bg); }
  #android-power-profiler .chart-surface {
    background: var(--app-surface);
    border: 1px solid var(--app-border);
    border-radius: 6px;
    min-width: 0;
    overflow: hidden;
  }
  #android-power-profiler .chart-surface svg { display: block; width: 100%; height: auto; min-height: 260px; }
  #android-power-profiler .chart-surface .grid { stroke: var(--app-border); stroke-width: 1; }
  #android-power-profiler .chart-surface .axis-text, #android-power-profiler .chart-surface .lane-label { fill: var(--app-muted); font-size: 11px; }
  #android-power-profiler .chart-surface .lane-value { fill: var(--app-text); font-size: 11px; }
  #android-power-profiler .chart-surface .crosshair { stroke: var(--app-muted); stroke-width: 1; }
  #android-power-profiler .chart-surface .selected-point { fill: var(--app-bg); stroke-width: 2; }
  #android-power-profiler .chart-surface .event-span { fill: var(--series-3); opacity: .12; }
  #android-power-profiler .chart-surface .event-line { stroke: var(--series-3); stroke-width: 1; }
  #android-power-profiler .chart-surface .app-band { opacity: .78; }
  #android-power-profiler .chart-surface .band-label { fill: var(--app-text); font-size: 11px; }
  #android-power-profiler .sample-control { padding: 10px 12px 12px; border-top: 1px solid var(--app-border); }
  #android-power-profiler .sample-control input { width: 100%; accent-color: var(--series-1); }
  #android-power-profiler .sample-detail { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; color: var(--app-muted); font-size: 12px; }
  #android-power-profiler .sample-detail strong { color: var(--app-text); font-weight: 500; }
  #android-power-profiler .split-layout { display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(280px, .8fr); gap: 22px; }
  #android-power-profiler .data-table-wrap { overflow-x: auto; max-width: 100%; }
  #android-power-profiler table { width: 100%; border-collapse: collapse; min-width: 620px; }
  #android-power-profiler th, #android-power-profiler td { border-bottom: 1px solid var(--app-border); padding: 9px 8px; text-align: left; vertical-align: middle; font-size: 12px; }
  #android-power-profiler th { color: var(--app-muted); font-weight: 400; }
  #android-power-profiler td strong { display: block; font-weight: 500; }
  #android-power-profiler .cell-sub { display: block; margin-top: 2px; }
  #android-power-profiler .empty-cell { color: var(--app-muted); }
  #android-power-profiler .finding-list { display: grid; gap: 0; }
  #android-power-profiler .finding-row { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--app-border); }
  #android-power-profiler .finding-row strong { font-size: 13px; font-weight: 500; }
  #android-power-profiler .finding-row p { margin-top: 3px; overflow-wrap: anywhere; }
  #android-power-profiler .residency-list { display: grid; gap: 15px; }
  #android-power-profiler .residency-row { display: grid; grid-template-columns: 150px minmax(180px, 1fr) 150px; gap: 12px; align-items: center; }
  #android-power-profiler .residency-row > div:first-child { display: grid; }
  #android-power-profiler .residency-row > div:first-child span { color: var(--app-muted); font-size: 11px; }
  #android-power-profiler .stacked-bar { height: 10px; display: flex; overflow: hidden; background: var(--app-surface-2); }
  #android-power-profiler .band-low { background: var(--series-2); }
  #android-power-profiler .band-balanced { background: var(--series-1); }
  #android-power-profiler .band-high { background: var(--series-3); }
  #android-power-profiler .residency-values { display: flex; justify-content: flex-end; gap: 10px; color: var(--app-muted); font-size: 11px; }
  #android-power-profiler .status-line { gap: 9px; flex-wrap: wrap; }
  #android-power-profiler .status-line strong { margin-left: auto; font-size: 12px; font-weight: 500; }
  #android-power-profiler .availability-note { border-left: 3px solid var(--series-3); padding: 8px 11px; display: grid; gap: 3px; background: var(--app-surface); }
  #android-power-profiler .availability-note strong { font-size: 13px; font-weight: 500; }
  #android-power-profiler .contributor-list { display: grid; gap: 12px; }
  #android-power-profiler .contributor-row { display: grid; grid-template-columns: minmax(130px, .8fr) minmax(180px, 2fr) 70px; gap: 12px; align-items: center; }
  #android-power-profiler .contributor-row > div:first-child { display: grid; }
  #android-power-profiler .contributor-row span { color: var(--app-muted); font-size: 11px; }
  #android-power-profiler .bar-track { height: 8px; background: var(--app-surface-2); overflow: hidden; }
  #android-power-profiler .bar-track > span { display: block; height: 100%; background: var(--series-3); }
  #android-power-profiler .contributor-value { text-align: right; font-size: 12px; }
  #android-power-profiler .warning-list { margin: 0; padding-left: 18px; color: var(--series-3); font-size: 12px; }
  #android-power-profiler .metadata-block { margin: 0; padding: 13px; background: var(--app-surface); border: 1px solid var(--app-border); border-radius: 6px; color: var(--app-muted); white-space: pre-wrap; overflow-wrap: anywhere; font-size: 11px; }
  #android-power-profiler .legend-row { gap: 14px; flex-wrap: wrap; color: var(--app-muted); font-size: 11px; }
  #android-power-profiler .legend-row span { display: inline-flex; align-items: center; gap: 5px; }
  #android-power-profiler .legend-swatch { width: 9px; height: 9px; display: inline-block; }
  @media (max-width: 980px) {
    #android-power-profiler .metric-grid { grid-template-columns: repeat(2, minmax(150px, 1fr)); }
    #android-power-profiler .split-layout { grid-template-columns: 1fr; }
    #android-power-profiler .residency-row { grid-template-columns: 130px minmax(160px, 1fr); }
    #android-power-profiler .residency-values { grid-column: 2; justify-content: flex-start; }
  }
  @media (max-width: 720px) {
    #android-power-profiler .app-topbar { align-items: flex-start; flex-wrap: wrap; }
    #android-power-profiler .session-block { order: 3; width: 100%; }
    #android-power-profiler .app-workspace { grid-template-columns: 1fr; }
    #android-power-profiler .side-tabs { border-right: 0; border-bottom: 1px solid var(--app-border); flex-direction: row; overflow-x: auto; padding: 8px 10px; }
    #android-power-profiler .nav-tab { flex: 0 0 auto; text-align: center; }
    #android-power-profiler .nav-tab[aria-selected="true"] { box-shadow: inset 0 -2px 0 var(--series-1); }
    #android-power-profiler .app-content { padding: 16px 12px; }
    #android-power-profiler .metric-grid { grid-template-columns: 1fr 1fr; }
    #android-power-profiler .residency-row { grid-template-columns: 1fr; }
    #android-power-profiler .residency-values { grid-column: 1; }
    #android-power-profiler .contributor-row { grid-template-columns: minmax(0, 1fr) 65px; }
    #android-power-profiler .contributor-row .bar-track { grid-column: 1 / -1; grid-row: 2; }
  }
  @media (max-width: 440px) {
    #android-power-profiler .metric-grid { grid-template-columns: 1fr; }
    #android-power-profiler .device-block { width: 100%; justify-content: flex-start; text-align: left; }
    #android-power-profiler .metric-value { font-size: 22px; }
    #android-power-profiler .sample-detail { display: grid; grid-template-columns: 1fr; }
  }
</style>
<div id="android-power-profiler">
  <header class="app-topbar">
    <div class="brand-block">
      <span class="brand-mark">P</span>
      <div><strong>PowerScope Android</strong><span>Battery and resource profiler</span></div>
    </div>
    <div class="session-block">
      <span class="status-dot good"></span>
      <div><strong>@@TITLE@@</strong><span>@@TARGET@@ | @@DURATION@@ s | @@SAMPLES@@ samples</span></div>
    </div>
    <div class="device-block">
      <span class="status-dot"></span>
      <div><strong>@@DEVICE@@</strong><span>Android @@ANDROID@@ | @@SOC@@</span></div>
    </div>
  </header>
  <div class="app-workspace">
    <nav class="side-tabs" role="tablist" aria-label="Report views">
      <button type="button" class="nav-tab" role="tab" aria-selected="true" data-view="overview">Overview</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="timeline">Timeline</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="flow">Flow</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="applications">Applications</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="cpu">CPU</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="gpu">GPU</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="attribution">Attribution</button>
      <button type="button" class="nav-tab" role="tab" aria-selected="false" data-view="data">Data</button>
    </nav>
    <main class="app-content">
      <section class="app-view" data-panel="overview">
        <div class="view-heading"><div><h1>Session Overview</h1><p>@@GENERATED@@</p></div><span class="source-tag measured">fuel-gauge total</span></div>
        <div class="metric-grid">@@SUMMARY_CARDS@@</div>
        <section class="analysis-section">
          <div class="chart-toolbar">
            <div><h2>Measured Telemetry</h2><p class="section-copy">One device clock for current, voltage and resource counters.</p></div>
            <div class="segment-control" aria-label="Overview metric">
              <button type="button" class="segment-button active" data-overview-metric="power_mw">Power</button>
              <button type="button" class="segment-button" data-overview-metric="current_ma">Current</button>
              <button type="button" class="segment-button" data-overview-metric="cpu_pct">CPU</button>
              <button type="button" class="segment-button" data-overview-metric="voltage_mv">Voltage</button>
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
          <section class="analysis-section"><h2>Resource Summary</h2><div class="data-table-wrap"><table><thead><tr><th>CPU cluster</th><th>Load</th><th>Load-weighted freq</th><th>Observed / max</th><th>Modeled</th><th>Freq premium</th><th>Power assoc.</th></tr></thead><tbody>@@CPU_ROWS@@</tbody></table></div></section>
          <section class="analysis-section"><h2>Analysis</h2><div class="finding-list">@@FINDINGS@@</div></section>
        </div>
      </section>

      <section class="app-view" data-panel="timeline" hidden>
        <div class="view-heading"><div><h1>Aligned Timeline</h1><p>Measured total, CPU clusters and GPU evidence on one time axis.</p></div></div>
        <section class="analysis-section">
          <div class="chart-surface"><svg id="timeline-chart" role="img" aria-label="Aligned telemetry lanes"></svg></div>
        </section>
      </section>

      <section class="app-view" data-panel="flow" hidden>
        <div class="view-heading"><div><h1>Workflow Flow</h1><p>Foreground applications and imported test events aligned to measured battery power.</p></div><span class="source-tag counter">device uptime aligned</span></div>
        <section class="analysis-section">
          <div class="chart-surface"><svg id="flow-chart" role="img" aria-label="Power, foreground applications and external events on one timeline"></svg></div>
        </section>
        <section class="analysis-section"><h2>Phase Energy</h2><div class="data-table-wrap"><table><thead><tr><th>Phase / state</th><th>Count</th><th>Duration</th><th>Energy</th><th>Average</th><th>Confidence</th></tr></thead><tbody>@@PHASE_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>Five-minute Windows</h2><div class="data-table-wrap"><table><thead><tr><th>Window</th><th>Coverage</th><th>Energy</th><th>Average</th><th>Dominant app</th></tr></thead><tbody>@@WINDOW_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>Imported Duration Events</h2><div class="data-table-wrap"><table><thead><tr><th>Start</th><th>Event</th><th>Duration</th><th>Energy</th><th>Confidence</th></tr></thead><tbody>@@EVENT_ROWS@@</tbody></table></div></section>
      </section>

      <section class="app-view" data-panel="applications" hidden>
        <div class="view-heading"><div><h1>Foreground Applications</h1><p>Measured battery-side energy allocated by the sampled foreground package.</p></div><span class="source-tag counter">@@APP_COVERAGE@@% context coverage</span></div>
        <section class="analysis-section"><h2>Application Energy</h2><div class="data-table-wrap"><table><thead><tr><th>Package</th><th>Duration</th><th>Time</th><th>Energy</th><th>Average</th><th>Entries</th><th>Confidence</th></tr></thead><tbody>@@APPLICATION_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>Foreground Transitions</h2><div class="data-table-wrap"><table><thead><tr><th>Elapsed</th><th>Package</th><th>Activity</th></tr></thead><tbody>@@TRANSITION_ROWS@@</tbody></table></div></section>
      </section>

      <section class="app-view" data-panel="cpu" hidden>
        <div class="view-heading"><div><h1>CPU Frequency Impact</h1><p>Per-core utilization joined with cpufreq and Android Power Profile current tables.</p></div><span class="source-tag model">model, not rail</span></div>
        <section class="analysis-section">
          <div class="chart-toolbar"><div><h2>Cluster Timeline</h2><p class="section-copy">Frequency, utilization and same-device modeled power.</p></div><div class="segment-control" aria-label="CPU cluster">@@CPU_SELECTORS@@</div></div>
          <div class="chart-surface"><svg id="cpu-chart" role="img" aria-label="CPU cluster frequency impact timeline"></svg></div>
        </section>
        <section class="analysis-section"><div class="chart-toolbar"><div><h2>Frequency Residency</h2><p class="section-copy">Load-weighted low, middle and high frequency exposure.</p></div><div class="legend-row"><span><i class="legend-swatch band-low"></i>Low</span><span><i class="legend-swatch band-balanced"></i>Middle</span><span><i class="legend-swatch band-high"></i>High</span></div></div><div class="residency-list">@@RESIDENCY_ROWS@@</div></section>
        <section class="analysis-section"><h2>Cluster Summary</h2><div class="data-table-wrap"><table><thead><tr><th>CPU cluster</th><th>Load</th><th>Load-weighted freq</th><th>Observed / max</th><th>Modeled</th><th>Freq premium</th><th>Power assoc.</th></tr></thead><tbody>@@CPU_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>Process CPU Snapshot</h2><div class="data-table-wrap"><table><thead><tr><th>Process</th><th>Total</th><th>User</th><th>Kernel</th></tr></thead><tbody>@@PROCESS_ROWS@@</tbody></table></div></section>
      </section>

      <section class="app-view" data-panel="gpu" hidden>
        <div class="view-heading"><div><h1>GPU Evidence</h1><p>Frequency when the OEM node is readable; driver UID work duration otherwise.</p></div></div>
        <section class="analysis-section"><div class="status-line">@@GPU_STATUS@@</div></section>
        <div>@@GPU_METRIC@@</div>
        <section class="analysis-section"><div class="chart-surface" id="gpu-chart-surface"><svg id="gpu-chart" role="img" aria-label="GPU telemetry timeline"></svg></div></section>
        <section class="analysis-section"><h2>GPU Work by UID</h2><div class="data-table-wrap"><table><thead><tr><th>Package / UID</th><th>Active</th><th>Run ratio</th><th>Source</th></tr></thead><tbody>@@GPU_UID_ROWS@@</tbody></table></div></section>
      </section>

      <section class="app-view" data-panel="attribution" hidden>
        <div class="view-heading"><div><h1>Attribution</h1><p>Android model evidence is shown separately from measured battery output.</p></div><span class="source-tag model">non-additive estimates</span></div>
        <section class="analysis-section"><h2>Modeled Contributors</h2><div class="contributor-list">@@COMPONENT_ROWS@@</div></section>
        <section class="analysis-section"><h2>Top Attributed UIDs</h2><div class="data-table-wrap"><table><thead><tr><th>Package</th><th>UID</th><th>Modeled use</th><th>Leading components</th></tr></thead><tbody>@@UID_ROWS@@</tbody></table></div></section>
        <section class="analysis-section"><h2>Kernel Wakelocks</h2><div class="data-table-wrap"><table><thead><tr><th>Name</th><th>Duration</th><th>Count</th></tr></thead><tbody>@@WAKELOCK_ROWS@@</tbody></table></div></section>
      </section>

      <section class="app-view" data-panel="data" hidden>
        <div class="view-heading"><div><h1>Data Quality</h1><p>Measurement, counter and model provenance for this session.</p></div></div>
        <section class="analysis-section"><h2>Sources</h2><div class="data-table-wrap"><table><thead><tr><th>Metric</th><th>Source</th><th>Type</th></tr></thead><tbody>@@SOURCE_ROWS@@</tbody></table></div></section>
        @@WARNING_SECTION@@
        <section class="analysis-section"><h2>Session Metadata</h2><pre class="metadata-block">@@METADATA@@</pre></section>
      </section>
    </main>
  </div>
</div>
<script>
(() => {
  const root = document.getElementById("android-power-profiler");
  const bundle = @@DATA@@;
  const samples = bundle.samples || [];
  const contexts = (bundle.contexts || []).slice().sort((a, b) => Number(a.uptime_s) - Number(b.uptime_s));
  const events = (bundle.events || []).slice().sort((a, b) => Number(a.device_uptime_s) - Number(b.device_uptime_s));
  const analysis = bundle.analysis || {};
  const cpu = analysis.cpu || { clusters: [], timeline: [] };
  const gpu = analysis.gpu || {};
  const colors = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)"];
  let selectedIndex = 0;
  let overviewMetric = "power_mw";
  let selectedCluster = cpu.clusters.length ? cpu.clusters[0].name : null;

  const metricDefinitions = {
    power_mw: { label: "Power", unit: "mW", color: colors[0], value: sample => sample.power_mw },
    current_ma: { label: "Current magnitude", unit: "mA", color: colors[1], value: sample => sample.current_ma },
    cpu_pct: { label: "CPU", unit: "%", color: colors[2], value: sample => sample.cpu_pct },
    voltage_mv: { label: "Voltage", unit: "mV", color: colors[4], value: sample => sample.voltage_mv },
    gpu_frequency_mhz: { label: "GPU frequency", unit: "MHz", color: colors[5], value: sample => sample.gpu_frequency_mhz }
  };

  function svgNode(name, attrs = {}, text = "") {
    const node = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (text) node.textContent = text;
    return node;
  }
  function finite(value) { return value != null && Number.isFinite(Number(value)); }
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
  function attachOverlay(svg, width, height, left, right, top, bottom) {
    const overlay = svgNode("rect", { x: left, y: top, width: width - left - right, height: height - top - bottom, fill: "transparent" });
    overlay.addEventListener("mousemove", event => {
      const rect = svg.getBoundingClientRect();
      const localX = (event.clientX - rect.left) / rect.width * width;
      const time = Math.max(0, Math.min(maxTime(), (localX - left) / (width - left - right) * maxTime()));
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
      { label: "Power", unit: "mW", color: colors[0], value: sample => sample.power_mw },
      { label: "Current", unit: "mA", color: colors[1], value: sample => sample.current_ma },
      { label: "CPU total", unit: "%", color: colors[2], value: sample => sample.cpu_pct }
    ];
    cpu.clusters.forEach((cluster, index) => lanes.push({ label: `${cluster.label} freq`, unit: "MHz", color: colors[(index + 3) % colors.length], value: sample => (sample.frequencies_mhz || {})[cluster.name] }));
    if (gpu.frequency_available) lanes.push({ label: "GPU freq", unit: "MHz", color: colors[5], value: sample => sample.gpu_frequency_mhz });
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
    svg.appendChild(svgNode("text", { x: 12, y: top + 18, class: "lane-label" }, "Power"));
    svg.appendChild(svgNode("text", { x: 12, y: top + 38, class: "lane-value" }, format(powers[selectedIndex], "mW")));
    svg.appendChild(svgNode("text", { x: 12, y: bandTop + 20, class: "lane-label" }, "Foreground"));

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
    let currentPackage = currentContext && currentContext.foreground_package ? currentContext.foreground_package : "unknown";
    const segments = [];
    contexts.forEach(context => {
      const elapsed = Number(context.uptime_s) - startUptime;
      if (elapsed <= 0 || elapsed > maxTime()) return;
      const nextPackage = context.foreground_package || "unknown";
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

  function renderCpu() {
    const svg = root.querySelector("#cpu-chart");
    const cluster = cpu.clusters.find(item => item.name === selectedCluster);
    if (!svg || !cluster) return;
    const timeline = cpu.timeline || [];
    renderLanes(svg, [
      { label: `${cluster.label} freq`, unit: "MHz", color: colors[0], value: sample => (sample.frequencies_mhz || {})[cluster.name] },
      { label: `${cluster.label} load`, unit: "%", color: colors[1], value: sample => (sample.cluster_cpu_pct || {})[cluster.name] },
      { label: "Modeled CPU", unit: "mW", color: colors[2], value: (sample, index) => (((timeline[index] || {}).clusters || {})[cluster.name] || {}).modeled_power_mw },
      { label: "Freq premium", unit: "mW", color: colors[3], value: (sample, index) => (((timeline[index] || {}).clusters || {})[cluster.name] || {}).frequency_premium_mw }
    ]);
  }

  function renderGpu() {
    const surface = root.querySelector("#gpu-chart-surface");
    const svg = root.querySelector("#gpu-chart");
    if (!surface || !svg) return;
    if (!gpu.frequency_available) { surface.hidden = true; return; }
    const lanes = [{ label: "GPU frequency", unit: "MHz", color: colors[5], value: sample => sample.gpu_frequency_mhz }];
    if (samples.some(sample => finite(sample.gpu_load_pct))) lanes.push({ label: "GPU load", unit: "%", color: colors[1], value: sample => sample.gpu_load_pct });
    renderLanes(svg, lanes);
  }

  function updateSampleDetail() {
    const sample = samples[selectedIndex];
    const detail = root.querySelector("#sample-detail");
    const slider = root.querySelector("#overview-slider");
    if (!sample || !detail) return;
    if (slider) slider.value = String(selectedIndex);
    const context = contextForUptime(sample.uptime_s);
    const packageName = context && context.foreground_package ? context.foreground_package : "unknown";
    detail.innerHTML = `<span><strong>${formatTime(sample.elapsed_s)}</strong></span><span>Power <strong>${format(sample.power_mw, "mW")}</strong></span><span>Current <strong>${format(sample.current_ma, "mA")}</strong></span><span>CPU <strong>${format(sample.cpu_pct, "%")}</strong></span><span>App <strong>${packageName}</strong></span>`;
  }
  function selectSample(index) {
    selectedIndex = Math.max(0, Math.min(samples.length - 1, Number(index)));
    updateSampleDetail();
    renderOverview();
    renderTimeline();
    renderFlow();
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


def build_report_fragment(bundle: Dict[str, object]) -> str:
    bundle = _report_bundle(bundle)
    metadata = bundle.get("metadata", {})
    analysis = bundle.get("analysis", {})
    samples = bundle.get("samples", [])
    summary = analysis.get("summary", {}) if isinstance(analysis, dict) else {}
    device = metadata.get("device", {}) if isinstance(metadata, dict) else {}
    model = " ".join(
        str(part) for part in (device.get("brand"), device.get("model")) if part
    ) or "Android device"
    soc = device.get("soc_model") or device.get("hardware") or "unknown SoC"
    target = metadata.get("target_package")
    if not target:
        target = "multi-app session" if metadata.get("session_mode") else metadata.get("foreground_package") or "no target"
    cpu_rows, residency_rows, selectors = _cpu_rows(analysis)
    gpu_status, gpu_metric, gpu_uid_rows = _gpu_content(analysis)
    warnings = analysis.get("warnings", []) if isinstance(analysis, dict) else []
    warning_section = ""
    if warnings:
        warning_section = (
            '<section class="analysis-section"><h2>Collection Warnings</h2><ul class="warning-list">'
            + "".join(f"<li>{_escape(item)}</li>" for item in warnings)
            + "</ul></section>"
        )
    gpu_button = (
        '<button type="button" class="segment-button" data-overview-metric="gpu_frequency_mhz">GPU</button>'
        if isinstance(analysis.get("gpu"), dict) and analysis.get("gpu", {}).get("frequency_available")
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
        "@@DEVICE@@": _escape(model),
        "@@ANDROID@@": _escape(device.get("android", "unknown")),
        "@@SOC@@": _escape(soc),
        "@@GENERATED@@": _escape(metadata.get("generated_at", "")),
        "@@SUMMARY_CARDS@@": _summary_cards(summary),
        "@@GPU_METRIC_BUTTON@@": gpu_button,
        "@@SLIDER_MAX@@": str(max(0, len(samples) - 1)),
        "@@CPU_ROWS@@": cpu_rows,
        "@@CPU_SELECTORS@@": selectors,
        "@@RESIDENCY_ROWS@@": residency_rows,
        "@@PROCESS_ROWS@@": _process_rows(analysis),
        "@@FINDINGS@@": _finding_rows(analysis),
        "@@GPU_STATUS@@": gpu_status,
        "@@GPU_METRIC@@": gpu_metric,
        "@@GPU_UID_ROWS@@": gpu_uid_rows,
        "@@COMPONENT_ROWS@@": _component_rows(analysis),
        "@@UID_ROWS@@": _uid_rows(analysis),
        "@@WAKELOCK_ROWS@@": _wakelock_rows(analysis),
        "@@APPLICATION_ROWS@@": _application_rows(analysis),
        "@@TRANSITION_ROWS@@": _transition_rows(analysis),
        "@@PHASE_ROWS@@": _phase_rows(analysis),
        "@@EVENT_ROWS@@": _event_rows(analysis),
        "@@WINDOW_ROWS@@": _window_rows(analysis),
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
<html lang="en">
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
