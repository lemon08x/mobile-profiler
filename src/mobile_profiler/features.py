from __future__ import annotations

from typing import Dict, Iterable, Mapping, Sequence


CAPTURE_FEATURES: Dict[str, Dict[str, str]] = {
    "cpu_usage": {
        "label": "CPU 利用率",
        "description": "记录整机与可用的逐核/集群 CPU 负载。",
        "overhead": "low",
    },
    "cpu_frequency": {
        "label": "CPU 频率",
        "description": "读取 CPU 集群频率与频率上限。",
        "overhead": "low",
    },
    "gpu_metrics": {
        "label": "GPU 指标",
        "description": "读取 GPU 频率、负载与渲染器信息。",
        "overhead": "low",
    },
    "memory_frequency": {
        "label": "内存频率",
        "description": "读取 DDR/DRAM/DMC/MIF 频率。",
        "overhead": "low",
        "default_off_reason": "量产设备常不暴露可读内存频率节点，仅在 Probe 确认可用时按需开启",
    },
    "foreground_window": {
        "label": "前台应用与窗口",
        "description": "跟踪前台包名、窗口、分辨率、亮度和刷新率。",
        "overhead": "medium",
    },
    "frame_rate": {
        "label": "帧率与帧间隔",
        "description": "记录 FPS、帧间隔、1% Low 与异常帧。",
        "overhead": "medium",
    },
    "frame_details": {
        "label": "详细帧时间戳",
        "description": "采集 Android framestats 或 SmartPerf 帧抖动明细。",
        "overhead": "high",
    },
    "harmony_hitches": {
        "label": "Harmony hitch 统计",
        "description": "读取前台 RenderService 窗口的 hitch 累计计数。",
        "overhead": "medium",
        "default_off_reason": "当前报告不使用累计 hitch 形成独立结论，仅在专项排查时开启",
    },
    "touch_events": {
        "label": "触控事件",
        "description": "记录系统已分发的触控事件；不推断面板硬件采样率。",
        "overhead": "medium",
        "default_off_reason": "只能统计系统已分发事件，不能得到硬件触控采样率或稳定性能结论",
    },
    "target_process": {
        "label": "目标应用资源",
        "description": "在支持的后端记录目标游戏进程的 CPU 与内存快照。",
        "overhead": "low",
        "default_off_reason": "当前报告没有目标进程 CPU/内存时间序列或独立结论，专项排查时开启",
    },
    "process_snapshots": {
        "label": "全系统进程快照",
        "description": "周期扫描全系统进程，用于发现后台竞争与三方负载。",
        "overhead": "high",
        "default_off_reason": "全系统轮询开销较高，结果主要是时间相关性上下文，默认关闭",
    },
    "hot_threads": {
        "label": "热点线程快照",
        "description": "周期扫描热点线程与渲染/合成线程。",
        "overhead": "high",
        "default_off_reason": "仅在已经出现卡顿后用于线程定位，默认关闭以降低观察者开销",
    },
    "thermal": {
        "label": "温度与热限制",
        "description": "记录 ThermalService 或 SmartPerf 温度传感器。",
        "overhead": "low",
    },
    "scheduler": {
        "label": "调度与资源分配",
        "description": "记录 cpuset、Governor、进程状态、ADPF 或 PowerManager 上下文。",
        "overhead": "high",
        "default_off_reason": "当前只能提供调度状态上下文，不能独立判断帧率或功耗因果，默认关闭",
    },
    "runtime_settings": {
        "label": "系统设置快照",
        "description": "在测试前后读取亮度、刷新率、网络、定位等设置。",
        "overhead": "medium",
        "default_off_reason": "仅用于测试前后条件核对；性能模式默认关闭",
    },
    "power_attribution": {
        "label": "功耗来源归因",
        "description": "采集 BatteryStats、UID、Wakelock 与组件功耗模型。",
        "overhead": "high",
        "default_off_reason": "BatteryStats 与组件模型不是物理电源轨，且会重置并执行重型收尾命令，默认关闭",
    },
}


CAPTURE_PRESET_LABELS = {
    "auto": "跟随测试模式",
    "power-standard": "功耗标准",
    "performance-standard": "性能标准",
    "low-overhead": "低干扰",
    "harmony-smartperf": "Harmony SmartPerf 采集",
}


PLATFORM_UNAVAILABLE_FEATURES: Dict[str, Dict[str, str]] = {
    "android": {
        "harmony_hitches": "Android 使用 gfxinfo/SurfaceFlinger，不读取 Harmony RenderService hitch",
        "touch_events": "当前 Android 会话采集器不产生可分析的触控事件时间序列",
    },
    "harmony": {
        "hot_threads": "当前 HarmonyOS 量产接口不启用全系统线程扫描",
        "runtime_settings": "HarmonyOS 量产 HDC 不使用 Android settings 快照",
        "power_attribution": "HarmonyOS 不提供 Android BatteryStats/UID/Wakelock 归因",
    },
    "ios": {
        "cpu_frequency": "iOS DVT sidecar 不公开 CPU 集群频率",
        "memory_frequency": "iOS DVT sidecar 不公开 DRAM/内存控制器频率",
        "frame_rate": "当前 iOS sidecar 不提供通用应用 FPS 计数",
        "frame_details": "当前 iOS sidecar 不提供 Core Animation 详细帧时间戳",
        "harmony_hitches": "Harmony RenderService hitch 不适用于 iOS",
        "touch_events": "当前 iOS sidecar 不采集系统触控事件",
        "target_process": "当前 iOS sidecar 不提供由该开关单独控制的目标进程时间序列",
        "hot_threads": "当前 iOS sidecar 提供进程快照，不提供全系统线程扫描",
        "scheduler": "iOS 量产接口不公开 Android cpuset/ADPF 调度状态",
        "runtime_settings": "当前 iOS sidecar 不采集 Android settings 类型快照",
        "power_attribution": "iOS DVT powerScore 是相对诊断分数，不作为物理功耗来源归因",
    },
}


PLATFORM_REQUIRED_FEATURES: Dict[str, Dict[str, str]] = {
    "ios": {
        "cpu_usage": "iOS DVT 基础流固定包含 CPU 使用率",
        "gpu_metrics": "iOS DVT 基础流固定包含 Graphics 利用率",
        "foreground_window": "iOS application-state 基础流固定包含前台应用状态",
        "thermal": "iOS 电池诊断基础流固定包含电池温度",
    },
}


def _feature_map(enabled: Iterable[str]) -> Dict[str, bool]:
    selected = set(enabled)
    return {name: name in selected for name in CAPTURE_FEATURES}


_POWER_STANDARD = _feature_map(
    {
        "cpu_usage",
        "cpu_frequency",
        "gpu_metrics",
        "foreground_window",
        "thermal",
        "runtime_settings",
    }
)

_PERFORMANCE_STANDARD = _feature_map(
    {
        "cpu_usage",
        "cpu_frequency",
        "gpu_metrics",
        "foreground_window",
        "frame_rate",
        "frame_details",
        "thermal",
    }
)

_HARMONY_SMARTPERF = _feature_map(
    {
        "cpu_usage",
        "cpu_frequency",
        "gpu_metrics",
        "foreground_window",
        "frame_rate",
        "frame_details",
        "target_process",
        "thermal",
    }
)


def capture_feature_names() -> tuple[str, ...]:
    return tuple(CAPTURE_FEATURES)


def capture_preset_names() -> tuple[str, ...]:
    return tuple(CAPTURE_PRESET_LABELS)


def _preset_features(preset: str, test_mode: str) -> Dict[str, bool]:
    if preset == "power-standard":
        return dict(_POWER_STANDARD)
    if preset == "performance-standard":
        return dict(_PERFORMANCE_STANDARD)
    if preset == "harmony-smartperf":
        return dict(_HARMONY_SMARTPERF)
    if preset == "low-overhead":
        enabled = {"cpu_usage", "foreground_window"}
        if test_mode == "performance":
            enabled.update({"frame_rate", "thermal"})
        return _feature_map(enabled)
    raise ValueError(f"unknown capture preset: {preset}")


def resolve_capture_configuration(
    test_mode: str,
    platform: str,
    requested_preset: str = "auto",
    *,
    enable_features: Sequence[str] = (),
    disable_features: Sequence[str] = (),
    legacy_system_monitor_enabled: bool | None = None,
) -> Dict[str, object]:
    mode = str(test_mode or "power").strip().lower()
    if mode not in {"power", "performance"}:
        raise ValueError("test mode must be power or performance")
    platform_name = str(platform or "android").strip().lower()
    preset = str(requested_preset or "auto").strip().lower()
    if preset not in CAPTURE_PRESET_LABELS:
        raise ValueError(f"unknown capture preset: {preset}")

    effective_preset = (
        "performance-standard" if preset == "auto" and mode == "performance"
        else "power-standard" if preset == "auto"
        else preset
    )
    if effective_preset == "harmony-smartperf" and platform_name != "harmony":
        raise ValueError("Harmony SmartPerf preset requires a HarmonyOS HDC device")
    if effective_preset == "harmony-smartperf" and mode != "performance":
        raise ValueError("Harmony SmartPerf preset is available only in performance mode")

    features = _preset_features(effective_preset, mode)
    reasons: Dict[str, str] = {}
    for name, enabled in features.items():
        definition = CAPTURE_FEATURES[name]
        reasons[name] = (
            f"由“{CAPTURE_PRESET_LABELS[effective_preset]}”预设启用"
            if enabled
            else definition.get("default_off_reason")
            or f"未包含在“{CAPTURE_PRESET_LABELS[effective_preset]}”预设中"
        )

    for name in enable_features:
        if name not in CAPTURE_FEATURES:
            raise ValueError(f"unknown capture feature: {name}")
        features[name] = True
        reasons[name] = "用户显式启用"
    for name in disable_features:
        if name not in CAPTURE_FEATURES:
            raise ValueError(f"unknown capture feature: {name}")
        features[name] = False
        reasons[name] = "用户显式关闭以降低采集干扰"

    if legacy_system_monitor_enabled is False:
        for name in ("process_snapshots", "hot_threads", "thermal", "scheduler"):
            features[name] = False
            reasons[name] = "兼容 --no-system-monitor：扩展系统监控已关闭"

    explicit_disabled = set(disable_features)
    if "frame_rate" in explicit_disabled and features["frame_details"]:
        features["frame_details"] = False
        reasons["frame_details"] = "帧率采集已关闭，详细帧时间戳随之关闭"
    elif features["frame_details"] and not features["frame_rate"]:
        features["frame_rate"] = True
        reasons["frame_rate"] = "详细帧时间戳依赖帧率采集，已自动启用"
    if "foreground_window" in explicit_disabled and features["harmony_hitches"]:
        features["harmony_hitches"] = False
        reasons["harmony_hitches"] = "前台窗口采集已关闭，hitch 统计随之关闭"
    elif features["harmony_hitches"] and not features["foreground_window"]:
        features["foreground_window"] = True
        reasons["foreground_window"] = "Harmony hitch 统计需要前台窗口，已自动启用"
    if "process_snapshots" in explicit_disabled and features["hot_threads"]:
        features["hot_threads"] = False
        reasons["hot_threads"] = "全系统进程快照已关闭，热点线程随之关闭"
    elif features["hot_threads"] and not features["process_snapshots"]:
        features["process_snapshots"] = True
        reasons["process_snapshots"] = "热点线程快照同时需要进程上下文，已自动启用"

    if mode == "performance" and features["power_attribution"]:
        features["power_attribution"] = False
        reasons["power_attribution"] = "性能模式只记录整机功耗，不执行来源归因"
    for name, reason in PLATFORM_UNAVAILABLE_FEATURES.get(platform_name, {}).items():
        if features[name]:
            features[name] = False
        reasons[name] = reason
    if platform_name == "harmony" and effective_preset != "harmony-smartperf":
        harmony_native_unavailable = {
            "gpu_metrics": "HarmonyOS 原生 HDC 后端当前没有会话内 GPU 频率或负载时间序列",
            "memory_frequency": "HarmonyOS 原生 HDC 后端当前没有会话内 DDR 频率时间序列",
            "frame_details": "HarmonyOS 原生 HDC 后端当前不产生可分析的详细帧阶段时间戳",
            "target_process": "HarmonyOS 原生 HDC 后端当前不产生目标进程 CPU/内存时间序列",
        }
        for name, reason in harmony_native_unavailable.items():
            features[name] = False
            reasons[name] = reason
    for name, reason in PLATFORM_REQUIRED_FEATURES.get(platform_name, {}).items():
        features[name] = True
        reasons[name] = reason

    backend = (
        "harmony_smartperf"
        if effective_preset == "harmony-smartperf"
        else "platform_native"
    )
    rows = [
        {
            "key": name,
            "label": definition["label"],
            "description": definition["description"],
            "overhead": definition["overhead"],
            "enabled": bool(features[name]),
            "reason": reasons[name],
        }
        for name, definition in CAPTURE_FEATURES.items()
    ]
    return {
        "requested_preset": preset,
        "preset": effective_preset,
        "preset_label": CAPTURE_PRESET_LABELS[effective_preset],
        "backend": backend,
        "features": features,
        "feature_rows": rows,
        "enabled_count": sum(1 for enabled in features.values() if enabled),
        "disabled_count": sum(1 for enabled in features.values() if not enabled),
        "base_channels": ["battery_current", "battery_voltage", "device_timestamp"],
    }


def capture_features_from_metadata(metadata: Mapping[str, object]) -> Dict[str, bool]:
    configuration = metadata.get("capture_configuration", {})
    if isinstance(configuration, Mapping):
        values = configuration.get("features", {})
        if isinstance(values, Mapping) and any(name in values for name in CAPTURE_FEATURES):
            return {
                name: bool(values.get(name, True))
                for name in CAPTURE_FEATURES
            }
    return {name: True for name in CAPTURE_FEATURES}
