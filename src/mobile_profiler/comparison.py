from __future__ import annotations

import html
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from .storage import load_existing_run


def _number(value: object) -> Optional[float]:
    if not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _nested(value: object, *keys: str) -> object:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _delta(a: object, b: object) -> Dict[str, Optional[float]]:
    left = _number(a)
    right = _number(b)
    if left is None or right is None:
        return {"absolute": None, "percent": None}
    absolute = right - left
    percent = absolute / left * 100.0 if abs(left) > 1e-12 else None
    return {"absolute": absolute, "percent": percent}


def _device_name(metadata: Dict[str, object], fallback: str) -> str:
    device = metadata.get("device", {})
    device = device if isinstance(device, dict) else {}
    name = " ".join(
        str(value)
        for value in (device.get("brand"), device.get("model"))
        if value
    ).strip()
    return name or str(metadata.get("device_id") or metadata.get("adb_serial") or fallback)


def _device_identifier(metadata: Dict[str, object]) -> str:
    return str(
        metadata.get("device_id")
        or metadata.get("adb_serial")
        or metadata.get("ios_udid")
        or "—"
    )


def _platform_hardware(metadata: Dict[str, object]) -> str:
    platform = str(metadata.get("platform") or "android").lower()
    device = metadata.get("device", {})
    device = device if isinstance(device, dict) else {}
    if platform == "ios":
        version = device.get("ios") or "—"
        hardware = device.get("hardware") or device.get("product_type") or "—"
        return f"iOS {version} / {hardware}"
    if platform == "harmony":
        version = device.get("harmony") or device.get("openharmony") or "—"
        hardware = device.get("soc_model") or device.get("hardware") or "—"
        return f"HarmonyOS {version} / {hardware}"
    version = device.get("android") or "—"
    hardware = device.get("soc_model") or device.get("hardware") or "—"
    return f"Android {version} / {hardware}"


def _test_item_map(analysis: Dict[str, object]) -> Dict[Tuple[str, str], Dict[str, object]]:
    test_items = analysis.get("test_items", {})
    rows = test_items.get("rows", []) if isinstance(test_items, dict) else []
    result: Dict[Tuple[str, str], Dict[str, object]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        phase = str(item.get("phase") or "测试")
        name = str(item.get("name") or "未命名测试项")
        comparison_key = str(item.get("comparison_key") or "")
        if not comparison_key:
            comparison_key = name.split(" / ", 1)[0] if phase in {"test", "测试"} else name
        result[(phase, comparison_key)] = item
    return result


def _activity_overlap_pct(item: Optional[Dict[str, object]], family: str) -> Optional[float]:
    if not item:
        return None
    duration = _number(item.get("duration_s"))
    group = item.get(family, {})
    overlap = _number(group.get("overlap_s")) if isinstance(group, dict) else None
    if duration is None or duration <= 0 or overlap is None:
        return None
    return overlap / duration * 100.0


def _top_processes(item: Optional[Dict[str, object]]) -> Sequence[str]:
    if not item:
        return []
    rows = item.get("top_processes", [])
    if not isinstance(rows, list):
        return []
    return [str(row.get("name")) for row in rows[:5] if isinstance(row, dict) and row.get("name")]


def _gpu_item_text(item: Dict[str, object]) -> str:
    load = _number(item.get("average_gpu_load_pct"))
    if load is not None:
        return f"{load:.1f}%"
    frequency = _number(item.get("average_gpu_frequency_mhz"))
    return f"{frequency:.0f} MHz" if frequency is not None else "—"


def build_run_comparison(
    run_a: Path,
    run_b: Path,
    label_a: str = "",
    label_b: str = "",
    title: str = "双机续航与系统活动对比",
) -> Dict[str, object]:
    metadata_a, analysis_a, _ = load_existing_run(run_a)
    metadata_b, analysis_b, _ = load_existing_run(run_b)
    label_a = label_a or _device_name(metadata_a, run_a.name)
    label_b = label_b or _device_name(metadata_b, run_b.name)
    if label_a == label_b:
        label_a = f"{label_a} · {run_a.name}"
        label_b = f"{label_b} · {run_b.name}"
    summary_a = analysis_a.get("summary", {})
    summary_b = analysis_b.get("summary", {})
    summary_a = summary_a if isinstance(summary_a, dict) else {}
    summary_b = summary_b if isinstance(summary_b, dict) else {}
    consumption_representative_a = (
        summary_a.get("consumption_session_representative") is True
    )
    consumption_representative_b = (
        summary_b.get("consumption_session_representative") is True
    )
    power_comparison_available = (
        consumption_representative_a and consumption_representative_b
    )

    metric_specs = [
        ("average_power_mw", "平均功率", "mW", True),
        ("p95_power_mw", "P95 功率", "mW", True),
        ("maximum_power_mw", "峰值功率", "mW", True),
        ("energy_per_minute_mwh", "每分钟能量", "mWh/min", True),
        ("average_current_ma", "平均放电电流", "mA", True),
        ("average_cpu_pct", "平均 CPU", "%", None),
        ("maximum_cpu_pct", "峰值 CPU", "%", None),
        ("temperature_delta_c", "电池温升", "°C", True),
        ("coverage_pct", "遥测覆盖率", "%", False),
    ]
    consumption_metric_keys = {
        "average_power_mw",
        "p95_power_mw",
        "maximum_power_mw",
        "energy_per_minute_mwh",
        "average_current_ma",
    }
    summary_rows = []
    for key, label, unit, lower_is_better in metric_specs:
        consumption_metric = key in consumption_metric_keys
        available = not consumption_metric or power_comparison_available
        value_a = summary_a.get(key) if available else None
        value_b = summary_b.get(key) if available else None
        difference = _delta(value_a, value_b)
        summary_rows.append(
            {
                "key": key,
                "label": label,
                "unit": unit,
                "a": value_a,
                "b": value_b,
                "delta": difference,
                "lower_is_better": lower_is_better,
                "available": available,
                "unavailable_reason": (
                    None
                    if available
                    else "两侧都必须是完整、连续且明确未接外部电源的放电会话。"
                ),
            }
        )

    map_a = _test_item_map(analysis_a)
    map_b = _test_item_map(analysis_b)
    test_rows = []
    for phase, name in sorted(set(map_a) | set(map_b)):
        item_a = map_a.get((phase, name))
        item_b = map_b.get((phase, name))
        item_power_available = bool(
            item_a
            and item_b
            and item_a.get("power_valid_for_consumption") is True
            and item_b.get("power_valid_for_consumption") is True
        )
        rate_a = item_a.get("mwh_per_minute") if item_power_available and item_a else None
        rate_b = item_b.get("mwh_per_minute") if item_power_available and item_b else None
        rate_delta = _delta(rate_a, rate_b)
        winner = None
        if _number(rate_a) is not None and _number(rate_b) is not None:
            if abs(float(rate_delta["absolute"] or 0.0)) < max(0.02, abs(float(rate_a)) * 0.005):
                winner = "接近"
            else:
                winner = label_a if float(rate_a) < float(rate_b) else label_b
        test_rows.append(
            {
                "phase": phase,
                "name": name,
                "matched": item_a is not None and item_b is not None,
                "a": item_a,
                "b": item_b,
                "duration_delta": _delta(
                    item_a.get("duration_s") if item_a else None,
                    item_b.get("duration_s") if item_b else None,
                ),
                "energy_rate_delta": rate_delta,
                "average_power_delta": _delta(
                    item_a.get("average_power_mw") if item_power_available and item_a else None,
                    item_b.get("average_power_mw") if item_power_available and item_b else None,
                ),
                "p95_power_delta": _delta(
                    item_a.get("p95_power_mw") if item_power_available and item_a else None,
                    item_b.get("p95_power_mw") if item_power_available and item_b else None,
                ),
                "cpu_delta": _delta(
                    item_a.get("average_cpu_pct") if item_a else None,
                    item_b.get("average_cpu_pct") if item_b else None,
                ),
                "temperature_delta": _delta(
                    item_a.get("maximum_temperature_c") if item_a else None,
                    item_b.get("maximum_temperature_c") if item_b else None,
                ),
                "gc_overlap_pct_a": _activity_overlap_pct(item_a, "gc"),
                "gc_overlap_pct_b": _activity_overlap_pct(item_b, "gc"),
                "kworker_overlap_pct_a": _activity_overlap_pct(item_a, "kworker"),
                "kworker_overlap_pct_b": _activity_overlap_pct(item_b, "kworker"),
                "top_processes_a": _top_processes(item_a),
                "top_processes_b": _top_processes(item_b),
                "lower_energy_rate": winner,
                "power_comparison_available": item_power_available,
            }
        )

    warnings = []
    if not power_comparison_available:
        invalid_labels = [
            label
            for label, valid in (
                (label_a, consumption_representative_a),
                (label_b, consumption_representative_b),
            )
            if not valid
        ]
        warnings.append(
            "、".join(invalid_labels)
            + " 不是完整且明确的电池放电会话；平均功耗、P95、能量速率和低能耗侧均不比较。"
        )
    coverage_a = _number(summary_a.get("coverage_pct"))
    coverage_b = _number(summary_b.get("coverage_pct"))
    if coverage_a is not None and coverage_a < 90:
        warnings.append(f"{label_a} 遥测覆盖率仅 {coverage_a:.1f}%。")
    if coverage_b is not None and coverage_b < 90:
        warnings.append(f"{label_b} 遥测覆盖率仅 {coverage_b:.1f}%。")
    start_temp_a = _number(_nested(metadata_a, "battery_start", "temperature_c"))
    start_temp_b = _number(_nested(metadata_b, "battery_start", "temperature_c"))
    if start_temp_a is not None and start_temp_b is not None and abs(start_temp_b - start_temp_a) > 2.0:
        warnings.append(
            f"两台手机起始电池温度相差 {abs(start_temp_b - start_temp_a):.1f} °C，可能影响对比。"
        )
    interval_a = _number(metadata_a.get("sample_interval_s"))
    interval_b = _number(metadata_b.get("sample_interval_s"))
    if interval_a is not None and interval_b is not None and abs(interval_b - interval_a) > 0.01:
        warnings.append(f"两份运行的功率采样周期不同：{interval_a:g} s 与 {interval_b:g} s。")
    refresh_a = _number(_nested(analysis_a, "display", "active_refresh_hz"))
    refresh_b = _number(_nested(analysis_b, "display", "active_refresh_hz"))
    if refresh_a is not None and refresh_b is not None and abs(refresh_b - refresh_a) > 1.0:
        warnings.append(f"两台手机活动刷新率不同：{refresh_a:.0f} Hz 与 {refresh_b:.0f} Hz。")
    unmatched = [item for item in test_rows if not item["matched"]]
    if unmatched:
        warnings.append(f"有 {len(unmatched)} 个测试项只出现在其中一台手机，无法直接配对。")
    if not test_rows:
        warnings.append("两份报告都没有测试项矩阵；请先导入同一套 BTR2 持续事件日志。")
    matched = [item for item in test_rows if item["matched"]]
    duration_mismatch = [
        item
        for item in matched
        if isinstance(item["duration_delta"].get("percent"), (int, float))
        and abs(float(item["duration_delta"]["percent"])) > 5.0
    ]
    if duration_mismatch:
        warnings.append(f"有 {len(duration_mismatch)} 个配对测试项时长差超过 5%，优先比较 mWh/min 和平均功率。")

    return {
        "schema": "mobile-profiler-comparison-v2",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "title": title,
        "labels": {"a": label_a, "b": label_b},
        "runs": {
            "a": {
                "path": str(run_a.resolve()),
                "metadata": metadata_a,
                "summary": summary_a,
                "display": analysis_a.get("display", {}),
            },
            "b": {
                "path": str(run_b.resolve()),
                "metadata": metadata_b,
                "summary": summary_b,
                "display": analysis_b.get("display", {}),
            },
        },
        "summary_rows": summary_rows,
        "test_items": test_rows,
        "matched_test_item_count": len(matched),
        "power_comparison_available": power_comparison_available,
        "warnings": warnings,
        "interpretation": (
            "Positive delta means B minus A. Whole-device consumption power and mWh/min are compared only when both "
            "runs are complete, continuous, explicitly unplugged battery-discharge sessions. Process, GC, kworker, "
            "DEX/update and thermal values remain sampled temporal evidence rather than exclusive rail power."
        ),
    }


def _fmt(value: object, digits: int = 1) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:.{digits}f}"


def _delta_html(
    delta: object,
    unit: str,
    digits: int = 1,
    lower_is_better: Optional[bool] = True,
) -> str:
    if not isinstance(delta, dict):
        return "—"
    absolute = _number(delta.get("absolute"))
    percent = _number(delta.get("percent"))
    if absolute is None:
        return "—"
    if lower_is_better is None or absolute == 0:
        css = "neutral"
    elif lower_is_better:
        css = "worse" if absolute > 0 else "better"
    else:
        css = "better" if absolute > 0 else "worse"
    percent_text = f" ({percent:+.1f}%)" if percent is not None else ""
    return f'<span class="delta {css}">{absolute:+.{digits}f} {html.escape(unit)}{percent_text}</span>'


def _condition_rows(comparison: Dict[str, object]) -> str:
    runs = comparison["runs"]
    rows = []
    specs = [
        ("设备", lambda run: _device_name(run["metadata"], "未知设备")),
        ("设备标识", lambda run: _device_identifier(run["metadata"])),
        ("系统 / 硬件", lambda run: _platform_hardware(run["metadata"])),
        ("起始电量", lambda run: f"{_fmt(_nested(run['metadata'], 'battery_start', 'level_pct'), 0)}%"),
        ("起始电池温度", lambda run: f"{_fmt(_nested(run['metadata'], 'battery_start', 'temperature_c'))} °C"),
        ("采集起点", lambda run: f"{_nested(run['metadata'], 'capture_start', 'expected_context') or '—'} · {_nested(run['metadata'], 'capture_start', 'note') or '无备注'}"),
        ("采样周期", lambda run: f"{_fmt(run['metadata'].get('sample_interval_s'))} s"),
        ("遥测覆盖率", lambda run: f"{_fmt(run['summary'].get('coverage_pct'))}%"),
        ("显示刷新率", lambda run: f"{_fmt(run.get('display', {}).get('active_refresh_hz'), 0)} Hz"),
    ]
    for label, getter in specs:
        rows.append(
            "<tr>"
            f"<td><strong>{html.escape(label)}</strong></td>"
            f"<td>{html.escape(getter(runs['a']))}</td>"
            f"<td>{html.escape(getter(runs['b']))}</td>"
            "</tr>"
        )
    return "".join(rows)


def build_comparison_html(comparison: Dict[str, object]) -> str:
    labels = comparison["labels"]
    summary_rows = []
    for row in comparison["summary_rows"]:
        summary_rows.append(
            "<tr>"
            f"<td><strong>{html.escape(str(row['label']))}</strong></td>"
            f"<td>{_fmt(row.get('a'), 2)} {html.escape(str(row['unit']))}</td>"
            f"<td>{_fmt(row.get('b'), 2)} {html.escape(str(row['unit']))}</td>"
            f"<td>{_delta_html(row.get('delta'), str(row['unit']), 2, row.get('lower_is_better'))}</td>"
            "</tr>"
        )

    test_rows = []
    for row in comparison["test_items"]:
        item_a = row.get("a") or {}
        item_b = row.get("b") or {}
        power_item_a = item_a if row.get("power_comparison_available") is True else {}
        power_item_b = item_b if row.get("power_comparison_available") is True else {}
        test_rows.append(
            "<tr>"
            f"<td><strong>{html.escape(str(row.get('name')))}</strong><span>{html.escape(str(row.get('phase')))}</span></td>"
            f"<td>{_fmt(item_a.get('duration_s'))} / {_fmt(item_b.get('duration_s'))} s</td>"
            f"<td>{_fmt(power_item_a.get('mwh_per_minute'), 2)} / {_fmt(power_item_b.get('mwh_per_minute'), 2)}</td>"
            f"<td>{_fmt(power_item_a.get('average_power_mw'), 0)} / {_fmt(power_item_b.get('average_power_mw'), 0)} mW</td>"
            f"<td>{_delta_html(row.get('average_power_delta'), 'mW', 0)}</td>"
            f"<td>{_fmt(power_item_a.get('p95_power_mw'), 0)} / {_fmt(power_item_b.get('p95_power_mw'), 0)} mW</td>"
            f"<td>{_fmt(item_a.get('average_cpu_pct'))}% / {_fmt(item_b.get('average_cpu_pct'))}%</td>"
            f"<td>{html.escape(_gpu_item_text(item_a))} / {html.escape(_gpu_item_text(item_b))}</td>"
            f"<td>{_fmt(item_a.get('maximum_temperature_c'))} / {_fmt(item_b.get('maximum_temperature_c'))} °C</td>"
            f"<td>{_fmt(row.get('gc_overlap_pct_a'))}% / {_fmt(row.get('gc_overlap_pct_b'))}%</td>"
            f"<td>{_fmt(row.get('kworker_overlap_pct_a'))}% / {_fmt(row.get('kworker_overlap_pct_b'))}%</td>"
            f"<td>{_fmt(item_a.get('dex_update_overlap_s'))} / {_fmt(item_b.get('dex_update_overlap_s'))} s</td>"
            f"<td>{html.escape(str(item_a.get('interference_level') or '—'))} / {html.escape(str(item_b.get('interference_level') or '—'))}</td>"
            f"<td>{html.escape(', '.join(row.get('top_processes_a', [])[:3]) or '—')}<span>{html.escape(', '.join(row.get('top_processes_b', [])[:3]) or '—')}</span></td>"
            f"<td>{html.escape(str(row.get('lower_energy_rate') or '无法配对'))}</td>"
            "</tr>"
        )
    if not test_rows:
        test_rows.append('<tr><td colspan="15" class="empty">没有可配对的测试项。</td></tr>')
    warnings = comparison.get("warnings", [])
    warning_html = "".join(f"<li>{html.escape(str(item))}</li>" for item in warnings) or "<li>未发现明显的对比条件警告。</li>"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(str(comparison['title']))}</title><style>
:root{{--bg:#101316;--surface:#181d22;--surface2:#20262c;--border:#313942;--text:#eef2f5;--muted:#9ca8b3;--cyan:#50c6dc;--green:#65cf91;--orange:#f0a15e;--red:#e46f6f}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font-family:Inter,"Segoe UI",sans-serif}}main{{max-width:1680px;margin:auto;padding:26px}}header{{display:flex;justify-content:space-between;gap:20px;align-items:end;border-bottom:1px solid var(--border);padding-bottom:18px}}h1{{margin:0;font-size:26px;font-weight:560}}h2{{font-size:17px;font-weight:550;margin:0 0 12px}}p{{margin:5px 0 0;color:var(--muted);font-size:13px}}.labels{{display:flex;gap:8px;flex-wrap:wrap}}.tag{{border:1px solid var(--border);padding:6px 9px;border-radius:5px;color:var(--cyan);font-size:12px}}section{{margin-top:24px;border-top:1px solid var(--border);padding-top:17px}}.cards{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:20px}}.card{{padding:16px;border:1px solid var(--border);background:var(--surface);border-radius:7px}}.card strong{{font-size:18px;font-weight:550}}.card span{{display:block;margin-top:5px;color:var(--muted);font-size:12px}}.table-wrap{{overflow:auto;border:1px solid var(--border);border-radius:7px;background:var(--surface)}}table{{width:100%;border-collapse:collapse;min-width:760px}}th,td{{padding:10px 9px;border-bottom:1px solid var(--border);font-size:12px;text-align:left;vertical-align:top}}th{{color:var(--muted);font-weight:450;background:var(--surface2);position:sticky;top:0}}td strong{{display:block;font-weight:550}}td span{{display:block;color:var(--muted);margin-top:2px}}.delta{{display:inline!important;margin:0}}.delta.worse{{color:var(--red)}}.delta.better{{color:var(--green)}}.delta.neutral{{color:var(--muted)}}.warnings{{margin:0;padding-left:20px;color:var(--orange);font-size:13px}}.note{{border-left:3px solid var(--orange);padding:11px 13px;background:var(--surface);color:var(--muted);font-size:12px}}.empty{{color:var(--muted)}}@media(max-width:760px){{main{{padding:16px}}header{{align-items:flex-start;flex-direction:column}}.cards{{grid-template-columns:1fr}}}}
</style></head><body><main>
<header><div><h1>{html.escape(str(comparison['title']))}</h1><p>差值统一为 B - A；功率与能量越低通常越有利，但必须同时确认性能、温度和测试条件一致。</p></div><div class="labels"><span class="tag">A · {html.escape(str(labels['a']))}</span><span class="tag">B · {html.escape(str(labels['b']))}</span></div></header>
<div class="cards"><article class="card"><strong>A · {html.escape(str(labels['a']))}</strong><span>{html.escape(str(comparison['runs']['a']['path']))}</span></article><article class="card"><strong>B · {html.escape(str(labels['b']))}</strong><span>{html.escape(str(comparison['runs']['b']['path']))}</span></article></div>
<section><h2>测试条件</h2><div class="table-wrap"><table><thead><tr><th>条件</th><th>{html.escape(str(labels['a']))}</th><th>{html.escape(str(labels['b']))}</th></tr></thead><tbody>{_condition_rows(comparison)}</tbody></table></div></section>
<section><h2>整场指标</h2><div class="table-wrap"><table><thead><tr><th>指标</th><th>A</th><th>B</th><th>B - A</th></tr></thead><tbody>{''.join(summary_rows)}</tbody></table></div></section>
<section><h2>配对测试项</h2><div class="table-wrap"><table><thead><tr><th>测试项</th><th>时长 A/B</th><th>mWh/min A/B</th><th>平均功率 A/B</th><th>功率差</th><th>P95 A/B</th><th>CPU A/B</th><th>GPU A/B</th><th>传感器峰值 A/B</th><th>GC 重叠 A/B</th><th>kworker 重叠 A/B</th><th>DEX/更新 A/B</th><th>干扰 A/B</th><th>主要进程 A / B</th><th>低能耗侧</th></tr></thead><tbody>{''.join(test_rows)}</tbody></table></div></section>
<section><h2>对比警告</h2><ul class="warnings">{warning_html}</ul></section>
<section><div class="note">{html.escape(str(comparison['interpretation']))}</div></section>
</main></body></html>"""


def write_comparison(output_dir: Path, comparison: Dict[str, object]) -> Tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "comparison.json"
    report_path = output_dir / "comparison.html"
    json_path.write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(build_comparison_html(comparison), encoding="utf-8")
    return json_path, report_path
