from __future__ import annotations

import re


def localize_collection_warning(value: object) -> str:
    """Translate stable collector boundary messages for the user-facing UI/report."""

    text = str(value or "").strip()
    if not text:
        return ""

    exact = {
        (
            "iOS PowerTelemetryData.SystemLoad is a whole-device raw telemetry channel that typically "
            "refreshes about every 20 seconds; under external power it can track SystemPowerIn. It is "
            "neither battery I×V nor an independently measured hardware rail, and one-second CPU/GPU or "
            "power-score rows must not be treated as one-second SystemLoad measurements."
        ): (
            "iOS 的 SystemLoad 是整机原始遥测，通常约 20 秒刷新一次；外部供电时可能跟随 "
            "SystemPowerIn。它不是电池电流×电压，也不是独立硬件电源轨，不能把每秒 CPU/GPU "
            "或相对功耗分数记录误当作每秒功率样本。"
        ),
        (
            "iOS DVT sysmond/DTServiceHub/remotepairingdeviced add measurable collection overhead; "
            "collector_cpu_pct is retained in samples and profiler processes are tagged in system snapshots."
        ): (
            "iOS DVT 采集会让 sysmond、DTServiceHub 和配对服务产生可测 CPU 活动；报告保留其同期 "
            "CPU 上界，但不能把它直接当作采集工具的净开销。"
        ),
        "The iPhone is externally powered. Battery current is not a clean unplugged discharge measurement.": (
            "iPhone 当前接有外部电源；电池电流不是纯净的断电放电数据。"
        ),
        (
            "iOS DiagnosticsService did not expose ExternalConnected, so power samples remain raw telemetry "
            "until an explicit external-power state is available."
        ): (
            "iOS 诊断接口未返回是否外接电源，因此功率只作为原始通道展示，直到采到明确的供电状态。"
        ),
        (
            "HarmonyOS samples use the device realtime epoch because /proc/uptime is restricted to the HDC "
            "shell; all samples, contexts and snapshots remain in the same device clock domain."
        ): (
            "由于量产 HDC shell 不能读取 /proc/uptime，HarmonyOS 使用设备实时时钟；样本、上下文和"
            "快照仍处于同一时钟域，可以相互对齐。"
        ),
        (
            "HarmonyOS BatteryService reports whole-device battery current and voltage. Android BatteryStats, "
            "ADPF and dumpsys GPU attribution are not available and are not inferred."
        ): (
            "HarmonyOS BatteryService 提供整机电池电流和电压；系统不提供 Android BatteryStats、"
            "ADPF 或 dumpsys GPU 归因，报告不会推算这些数据。"
        ),
        (
            "Harmony SmartPerf uses native SP_daemon at its fixed approximately one-second cadence; enabled "
            "metrics are requested with -c/-g/-f/-t/-r/-d switches."
        ): (
            "Harmony SmartPerf 使用设备原生 SP_daemon，固定约 1 秒采样；只请求当前启用的指标。"
        ),
        (
            "SP_daemon was not available; collection fell back to RenderService, /proc/stat, top and hidumper "
            "sources."
        ): (
            "设备未提供 SP_daemon，已回退到 RenderService、/proc/stat、top 和 hidumper；可用指标会相应减少。"
        ),
        (
            "The HarmonyOS device is externally powered. Battery current is not a clean unplugged discharge "
            "measurement."
        ): (
            "HarmonyOS 设备当前接有外部电源；电池电流不是纯净的断电放电数据。"
        ),
        (
            "HarmonyOS BatteryService did not expose pluggedType, so external-power state is unknown. Power "
            "samples remain raw battery flow until an explicit state is observed."
        ): (
            "HarmonyOS BatteryService 未返回插电类型，外部供电状态不确定；功率只作为电池侧原始流量展示。"
        ),
        "iPhone RemotePairing RemoteXPC endpoint did not recover before the reconnect timeout.": (
            "iPhone RemoteXPC 在重连等待时间内未恢复。"
        ),
        "SP_daemon emitted a record without usable timestamp/current/voltage.": (
            "SmartPerf 返回了一条缺少有效时间、电流或电压的记录，已忽略。"
        ),
        "HarmonyOS sampler emitted a frame without a timestamp.": (
            "HarmonyOS 原生采样返回了一帧缺少时间戳的数据，已忽略。"
        ),
    }
    if text in exact:
        return exact[text]

    match = re.fullmatch(
        r"Average normalized iOS collector CPU overhead was ([0-9.]+)% during this run\.",
        text,
    )
    if match:
        return (
            f"本次观察者相关进程的归一化 CPU 平均上界为 {match.group(1)}%；"
            "其中包含系统本底活动，不等于采集工具净开销。"
        )

    match = re.fullmatch(
        r"hidumper --cpufreq is sampled at a lower cadence than /proc/stat because a full 12-core dump is "
        r"comparatively expensive; the effective refresh cadence is about ([0-9.]+) seconds and intermediate "
        r"samples retain the latest value\.",
        text,
    )
    if match:
        return (
            "读取完整 12 核 CPU 频率快照的开销较高，因此频率约每 "
            f"{match.group(1)} 秒刷新一次；中间样本沿用最近值，并在图表中标明刷新节奏。"
        )

    match = re.fullmatch(
        r"HarmonyOS SP_daemon covered only ([0-9.]+)s of the requested ([0-9.]+)s window\.",
        text,
    )
    if match:
        return (
            f"SmartPerf 实际覆盖 {match.group(1)} 秒，短于请求的 {match.group(2)} 秒；"
            "本次记录按不完整数据处理。"
        )

    match = re.fullmatch(
        r"HarmonyOS performance mode restore failed; expected power mode ([0-9]+)\.",
        text,
    )
    if match:
        return f"HarmonyOS 性能模式恢复失败；预期恢复到模式 {match.group(1)}。"
    if text.startswith("HarmonyOS performance mode restore failed:"):
        return "HarmonyOS 性能模式恢复失败：" + text.split(":", 1)[1].strip()

    return text
