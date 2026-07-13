from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Set, Tuple


WATCHED_ACTIVITY_RULES: List[Dict[str, object]] = [
    {
        "name": "dex2oat",
        "kind": "dex_optimization",
        "label": "DEX AOT 编译",
        "impact": "ART 正在把字节码编译为本地代码，可能持续占用 CPU、存储 I/O 并推高温度。",
        "trigger": "presence",
        "pattern": re.compile(r"(?:^|[/\s])dex2oat(?:32|64)?(?:$|\s)", re.I),
    },
    {
        "name": "dexopt",
        "kind": "dex_optimization",
        "label": "DEX 优化",
        "impact": "应用安装或更新后的字节码优化可能产生较长的 CPU 与存储负载。",
        "trigger": "presence",
        "pattern": re.compile(r"\bdexopt(?:analyzer)?\b", re.I),
    },
    {
        "name": "profman",
        "kind": "dex_optimization",
        "label": "ART Profile 处理",
        "impact": "ART 正在合并或分析运行 Profile，用于指导后续编译。",
        "trigger": "presence",
        "pattern": re.compile(r"(?:^|[/\s])profman(?:$|\s)", re.I),
    },
    {
        "name": "odrefresh",
        "kind": "dex_optimization",
        "label": "Boot Classpath 刷新",
        "impact": "ART 正在检查或重新生成启动产物，常见于系统更新之后。",
        "trigger": "presence",
        "pattern": re.compile(r"(?:^|[/\s])odrefresh(?:$|\s)", re.I),
    },
    {
        "name": "otapreopt",
        "kind": "dex_optimization",
        "label": "OTA 预优化",
        "impact": "Android 正在为 OTA 槽位或更新后的系统镜像预优化应用包。",
        "trigger": "presence",
        "pattern": re.compile(r"\botapreopt(?:_chroot)?\b", re.I),
    },
    {
        "name": "artd",
        "kind": "dex_optimization",
        "label": "ART 服务活动",
        "impact": "ART daemon 负责协调 DEX 优化，仅在 CPU 可见时判定为活动。",
        "trigger": "cpu",
        "pattern": re.compile(r"(?:^|[/\s])artd(?:$|\s)", re.I),
    },
    {
        "name": "installd",
        "kind": "package_management",
        "label": "安装包服务活动",
        "impact": "应用安装、迁移或产物维护可能占用 CPU 与存储带宽。",
        "trigger": "cpu",
        "pattern": re.compile(r"(?:^|[/\s])installd(?:$|\s)", re.I),
    },
    {
        "name": "update_engine",
        "kind": "system_update",
        "label": "系统更新引擎",
        "impact": "系统可能正在后台下载、校验或应用 OTA Payload。",
        "trigger": "cpu",
        "pattern": re.compile(r"\bupdate_engine(?:_sideload)?\b", re.I),
    },
    {
        "name": "update_verifier",
        "kind": "system_update",
        "label": "系统更新校验",
        "impact": "系统正在校验更新后的系统或应用产物。",
        "trigger": "cpu",
        "pattern": re.compile(r"\bupdate_verifier\b", re.I),
    },
    {
        "name": "apexd",
        "kind": "system_update",
        "label": "APEX 包服务",
        "impact": "Mainline/APEX 包可能正在暂存、校验或激活。",
        "trigger": "cpu",
        "pattern": re.compile(r"(?:^|[/\s])apexd(?:$|\s)", re.I),
    },
]


ART_GC_THREAD_PATTERN = re.compile(
    r"^(?:HeapTaskDaemon|GCDaemon|Concurrent\s+GC|GC(?:\s+Thread)?(?:[#:\s].*)?|"
    r"FinalizerDaemon|FinalizerWatchdogDaemon|FinalizerWatchd|ReferenceQueueDaemon|ReferenceQueueD)(?:\s.*)?$",
    re.I,
)

KWORKER_SUBSYSTEM_RULES: List[Tuple[str, str, re.Pattern[str], str]] = [
    (
        "storage",
        "存储 I/O",
        re.compile(r"(?:ufs|mmc|blk|scsi|nvme|f2fs|ext4|jbd2|writeback|flush|dm-|kblock|\bio\b)", re.I),
        "内核存储工作队列可能反映文件系统回写、块设备或闪存请求，会同时占用 CPU 与存储带宽。",
    ),
    (
        "display",
        "显示 / GPU",
        re.compile(r"(?:display|disp|drm|dpu|mdp|composer|\bhwc\b|frame|kgsl|mali|gpu)", re.I),
        "显示或 GPU 相关工作队列可能由合成、提交帧或驱动中断后的延迟工作触发。",
    ),
    (
        "network",
        "网络",
        re.compile(r"(?:net|napi|wlan|wifi|rmnet|ether|\brx\b|\btx\b)", re.I),
        "网络工作队列可能处理收发包、驱动回调或协议栈延迟任务。",
    ),
    (
        "memory",
        "内存",
        re.compile(r"(?:reclaim|compact|swap|\bmm\b|page|slab|oom)", re.I),
        "内存管理工作队列可能与回收、压缩、换页或页缓存维护有关。",
    ),
    (
        "power_thermal",
        "电源 / 热控",
        re.compile(r"(?:thermal|power|battery|cpufreq|devfreq|regulator|\bpm\b)", re.I),
        "电源或热控工作队列可能响应频率、电源域、温度或电池状态变化。",
    ),
]


def classify_thread_activity(name: str, process: str = "") -> Optional[Dict[str, str]]:
    """Classify high-value runtime/kernel thread activity without claiming causality."""
    thread_name = (name or "").strip().strip("[]")
    process_name = (process or "").strip().strip("[]")
    combined = f"{thread_name} {process_name}".strip()
    lower_name = thread_name.lower()
    lower_combined = combined.lower()

    if ART_GC_THREAD_PATTERN.match(thread_name):
        return {
            "activity_kind": "runtime_gc",
            "activity_family": "gc",
            "activity_label": "ART / GC",
            "activity_domain": "runtime",
            "subsystem": "art_runtime",
            "impact_hint": "ART 垃圾回收、终结器或引用队列活动可能造成 CPU 抖动、内存带宽占用和暂停风险。",
        }

    if re.match(r"^(?:kworker|yworker)(?:/|$)", lower_name):
        subsystem = "generic"
        label = "通用工作队列"
        impact = "kworker 正在执行内核延迟工作；仅凭线程名通常无法确定具体回调来源。"
        for candidate, candidate_label, pattern, candidate_impact in KWORKER_SUBSYSTEM_RULES:
            if pattern.search(combined):
                subsystem = candidate
                label = candidate_label
                impact = candidate_impact
                break
        kind = "kernel_workqueue" if subsystem == "generic" else f"kernel_workqueue_{subsystem}"
        return {
            "activity_kind": kind,
            "activity_family": "kworker",
            "activity_label": f"kworker · {label}",
            "activity_domain": "kernel",
            "subsystem": subsystem,
            "impact_hint": impact,
        }

    if re.match(r"^(?:rcu|rcuo|rcuc|rcub|rcuop|rcuog)", lower_name):
        return {
            "activity_kind": "kernel_rcu",
            "activity_family": "rcu",
            "activity_label": "RCU 回调",
            "activity_domain": "kernel",
            "subsystem": "rcu",
            "impact_hint": "RCU 回调积压或集中执行会形成可见内核 CPU 活动，常需结合前后系统负载判断。",
        }

    if lower_name.startswith("ksoftirqd/"):
        return {
            "activity_kind": "kernel_softirq",
            "activity_family": "irq",
            "activity_label": "softirq",
            "activity_domain": "kernel",
            "subsystem": "interrupt",
            "impact_hint": "ksoftirqd 高负载通常表示软中断工作被推迟到线程上下文，可能来自网络、存储或其他驱动。",
        }

    if re.match(r"^(?:irq/|irq-)", lower_name):
        return {
            "activity_kind": "kernel_irq",
            "activity_family": "irq",
            "activity_label": "IRQ 线程",
            "activity_domain": "kernel",
            "subsystem": "interrupt",
            "impact_hint": "线程化 IRQ 活动反映设备中断处理；具体来源需结合 IRQ 名称和同时发生的子系统活动。",
        }

    kernel_rules: List[Tuple[re.Pattern[str], str, str, str, str]] = [
        (
            re.compile(r"^(?:kblockd|jbd2/|mmcqd|scsi_eh_|ufs|writeback|flush-|f2fs)", re.I),
            "kernel_storage",
            "storage",
            "内核存储活动",
            "文件系统、块层或闪存后台活动可能增加 CPU、I/O 等待和整机功率。",
        ),
        (
            re.compile(r"^(?:kswapd|kcompactd|oom_reaper|khugepaged)", re.I),
            "kernel_memory",
            "memory_reclaim",
            "内存回收 / 压缩",
            "内存压力下的回收、压缩或换页活动会消耗 CPU 和内存带宽，并可能影响前台流畅度。",
        ),
        (
            re.compile(r"^(?:migration/|cpuhp/|idle_inject/|watchdog/)", re.I),
            "kernel_scheduler",
            "scheduler",
            "调度 / CPU 热插拔",
            "调度迁移、CPU hotplug 或 idle injection 活动可能与负载均衡及热控动作同时出现。",
        ),
        (
            re.compile(r"^(?:thermal|devfreq_wq|pm_)", re.I),
            "kernel_power_thermal",
            "power_thermal",
            "电源 / 热控内核活动",
            "电源与热控线程活动可作为频率、电源域或温度策略执行的旁证。",
        ),
    ]
    for pattern, kind, subsystem, label, impact in kernel_rules:
        if pattern.search(thread_name):
            return {
                "activity_kind": kind,
                "activity_family": subsystem,
                "activity_label": label,
                "activity_domain": "kernel",
                "subsystem": subsystem,
                "impact_hint": impact,
            }

    if (
        re.search(r"(?:surfaceflinger|android\.hardware\.graphics\.composer|composer@)", lower_combined)
        or lower_name in {"renderengine", "hwcomposer", "dispsync", "eventthread", "surfaceflinger"}
    ):
        return {
            "activity_kind": "display_composition",
            "activity_family": "display",
            "activity_label": "显示合成",
            "activity_domain": "system",
            "subsystem": "display",
            "impact_hint": "SurfaceFlinger、RenderEngine 或 Composer 活动通常与帧合成、显示提交和 GPU/显示驱动工作相关。",
        }
    return None


def first_number(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value)
    return float(match.group(0)) if match else None


def parse_key_values(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip().lower()] = value.strip()
    return values


def parse_battery(text: str) -> Dict[str, object]:
    values = parse_key_values(text)
    status_map = {
        1: "unknown",
        2: "charging",
        3: "discharging",
        4: "not_charging",
        5: "full",
    }
    status_value = first_number(values.get("status"))
    temperature = first_number(values.get("temperature"))
    powered = []
    for label, key in (
        ("AC", "ac powered"),
        ("USB", "usb powered"),
        ("wireless", "wireless powered"),
        ("dock", "dock powered"),
    ):
        if values.get(key, "").lower() == "true":
            powered.append(label)
    return {
        "level_pct": first_number(values.get("level")),
        "voltage_mv": first_number(values.get("voltage")),
        "temperature_c": temperature / 10.0 if temperature is not None else None,
        "charge_counter_uah": first_number(values.get("charge counter")),
        "status_code": int(status_value) if status_value is not None else None,
        "status": status_map.get(int(status_value), "unknown") if status_value is not None else "unknown",
        "powered": powered,
    }


def parse_duration_seconds(text: str) -> Optional[float]:
    if not text:
        return None
    total = 0.0
    found = False
    units = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(ms|d|h|m|s)(?![A-Za-z])", text, re.I):
        total += float(match.group(1)) * units[match.group(2).lower()]
        found = True
    return total if found else None


def extract_stats_window(text: str) -> Dict[str, Optional[float]]:
    values: Dict[str, Optional[float]] = {
        "time_on_battery_s": None,
        "screen_on_s": None,
        "mobile_radio_active_s": None,
    }
    patterns = {
        "time_on_battery_s": r"Time on battery:\s*([^\n(]+)",
        "screen_on_s": r"Screen on:\s*([^\n(]+)",
        "mobile_radio_active_s": r"Mobile radio active:\s*([^\n(]+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.I)
        if match:
            values[key] = parse_duration_seconds(match.group(1))
    return values


def _parse_profile_value(raw: str) -> object:
    value = raw.strip()
    if value.startswith("["):
        return [float(item) for item in re.findall(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", value)]
    token = value.split()[0] if value else ""
    try:
        return float(token)
    except ValueError:
        return token


def parse_power_profile(text: str) -> Dict[str, object]:
    """Parse scalar and multiline array entries from BatteryStats power profile."""

    profile: Dict[str, object] = {}
    pending_key: Optional[str] = None
    pending_value: List[str] = []

    def flush() -> None:
        nonlocal pending_key, pending_value
        if pending_key is not None:
            profile[pending_key] = _parse_profile_value(" ".join(pending_value))
        pending_key = None
        pending_value = []

    for line in text.splitlines():
        match = re.match(r"\s*([A-Za-z0-9_.]+)=(.*)$", line)
        if match:
            flush()
            key, raw = match.groups()
            if raw.lstrip().startswith("[") and "]" not in raw:
                pending_key = key
                pending_value = [raw]
            else:
                profile[key] = _parse_profile_value(raw)
            continue
        if pending_key is not None:
            pending_value.append(line.strip())
            if "]" in line:
                flush()
    flush()
    return profile


def uid_token_to_number(token: str) -> Optional[int]:
    if token.isdigit():
        return int(token)
    match = re.fullmatch(r"u(\d+)a(\d+)", token)
    if match:
        user_id = int(match.group(1))
        app_id = int(match.group(2))
        return user_id * 100000 + 10000 + app_id
    return None


def parse_package_uids(text: str) -> Tuple[Dict[int, List[str]], Dict[str, int]]:
    by_uid: Dict[int, List[str]] = {}
    by_package: Dict[str, int] = {}
    for line in text.splitlines():
        match = re.search(r"package:(\S+)\s+uid:(\d+)", line)
        if not match:
            continue
        package, uid_text = match.groups()
        uid = int(uid_text)
        by_uid.setdefault(uid, []).append(package)
        by_package[package] = uid
    return by_uid, by_package


def parse_battery_usage(text: str, package_uids: Dict[int, List[str]]) -> Dict[str, object]:
    result: Dict[str, object] = {
        "capacity_mah": None,
        "computed_drain_mah": None,
        "actual_drain_mah": None,
    }
    capacity_match = re.search(
        r"Capacity:\s*([\d.]+),\s*Computed drain:\s*([\d.]+),\s*actual drain:\s*([\d.]+)",
        text,
        re.I,
    )
    if capacity_match:
        result["capacity_mah"] = float(capacity_match.group(1))
        result["computed_drain_mah"] = float(capacity_match.group(2))
        result["actual_drain_mah"] = float(capacity_match.group(3))

    components: Dict[str, Dict[str, object]] = {}
    lines = text.splitlines()
    in_global = False
    for line in lines:
        stripped = line.strip()
        if stripped == "Global":
            in_global = True
            continue
        if in_global and (stripped.startswith("UID ") or stripped.startswith("(")):
            break
        if in_global:
            match = re.match(r"\s+([A-Za-z][A-Za-z0-9_ -]*):\s*([-+\d.eE]+)(.*)", line)
            if not match:
                continue
            name, value_text, remainder = match.groups()
            try:
                value = float(value_text)
            except ValueError:
                continue
            duration_match = re.search(r"duration:\s*([^\n]+)$", remainder)
            components[name.strip()] = {
                "name": name.strip(),
                "mah": value,
                "duration_s": parse_duration_seconds(duration_match.group(1)) if duration_match else None,
                "source": "BatteryStats model",
            }

    uid_entries: List[Dict[str, object]] = []
    current: Optional[Dict[str, object]] = None
    for line in lines:
        uid_match = re.match(r"\s*UID\s+([^:]+):\s*([-+\d.eE]+)(.*)", line)
        if uid_match:
            token, value_text, remainder = uid_match.groups()
            uid_number = uid_token_to_number(token)
            current = {
                "token": token,
                "uid": uid_number,
                "packages": package_uids.get(uid_number or -1, []),
                "mah": float(value_text),
                "state_summary": remainder.strip(),
                "components": {},
            }
            uid_entries.append(current)
            continue
        if current is None or line.lstrip().startswith("("):
            continue
        if re.match(r"^ {6}[^\s]+=", line):
            for component_match in re.finditer(r"\b([A-Za-z][\w:]*)=([-+\d.eE]+)", line):
                name, value_text = component_match.groups()
                if ":" in name:
                    continue
                try:
                    current["components"][name] = float(value_text)  # type: ignore[index]
                except ValueError:
                    continue
    uid_entries.sort(key=lambda item: float(item.get("mah", 0.0)), reverse=True)
    result["components"] = list(components.values())
    result["uids"] = uid_entries
    return result


def parse_checkin_network(text: str, target_uid: Optional[int]) -> Optional[Dict[str, int]]:
    if target_uid is None:
        return None
    prefix = f",{target_uid},"
    for line in text.splitlines():
        if prefix not in line or ",nt," not in line:
            continue
        parts = [part.strip() for part in line.split(",")]
        try:
            nt_index = parts.index("nt")
        except ValueError:
            continue
        numbers: List[int] = []
        for value in parts[nt_index + 1 :]:
            try:
                numbers.append(int(value))
            except ValueError:
                numbers.append(0)
        if len(numbers) < 6:
            return None
        return {
            "mobile_rx_bytes": numbers[0],
            "mobile_tx_bytes": numbers[1],
            "wifi_rx_bytes": numbers[2],
            "wifi_tx_bytes": numbers[3],
            "bluetooth_rx_bytes": numbers[4],
            "bluetooth_tx_bytes": numbers[5],
        }
    return None


def parse_cpu_processes(text: str) -> List[Dict[str, object]]:
    processes: List[Dict[str, object]] = []
    pattern = re.compile(
        r"^\s*([\d.]+)%\s+(\d+)/(.+?):\s*([\d.]+)%\s+user\s*\+\s*([\d.]+)%\s+kernel",
        re.M,
    )
    for match in pattern.finditer(text):
        total, pid, name, user, kernel = match.groups()
        processes.append(
            {
                "name": name,
                "pid": int(pid),
                "cpu_pct": float(total),
                "user_pct": float(user),
                "kernel_pct": float(kernel),
            }
        )
    return sorted(processes, key=lambda item: float(item["cpu_pct"]), reverse=True)[:20]


def parse_memory_bytes(value: str) -> Optional[int]:
    match = re.fullmatch(r"\s*([-+]?\d+(?:\.\d+)?)\s*([KMGTPE]?)(?:i?B)?\s*", value, re.I)
    if not match:
        return None
    amount = float(match.group(1))
    suffix = match.group(2).upper()
    multiplier = 1024 ** ("KMGTPE".find(suffix) + 1) if suffix else 1
    return max(0, int(round(amount * multiplier)))


def parse_cpu_time_seconds(value: str) -> Optional[float]:
    parts = value.strip().split(":")
    try:
        numbers = [float(item) for item in parts]
    except ValueError:
        return None
    if len(numbers) == 2:
        return numbers[0] * 60.0 + numbers[1]
    if len(numbers) == 3:
        return numbers[0] * 3600.0 + numbers[1] * 60.0 + numbers[2]
    if len(numbers) == 1 and math.isfinite(numbers[0]):
        return numbers[0]
    return None


def watched_activity_descriptor(command: str) -> Optional[Dict[str, str]]:
    for rule in WATCHED_ACTIVITY_RULES:
        pattern = rule.get("pattern")
        if isinstance(pattern, re.Pattern) and pattern.search(command or ""):
            return {
                "watch_name": str(rule["name"]),
                "watch_kind": str(rule["kind"]),
                "watch_label": str(rule["label"]),
                "watch_impact": str(rule["impact"]),
                "watch_trigger": str(rule["trigger"]),
            }
    return None


def _activity_is_active(
    descriptor: Optional[Dict[str, str]],
    cpu_pct: Optional[float],
    state: Optional[str],
) -> bool:
    if not descriptor:
        return False
    if descriptor.get("watch_trigger") == "presence":
        return True
    return (cpu_pct is not None and cpu_pct >= 0.5) or str(state or "").upper() in {"R", "D"}


def classify_process(user: str, command: str, ppid: Optional[int] = None) -> str:
    descriptor = watched_activity_descriptor(command)
    if descriptor:
        return str(descriptor["watch_kind"])
    lower = (command or "").lower()
    activity = classify_thread_activity((command or "").split(maxsplit=1)[0], command)
    if activity and activity.get("activity_domain") == "kernel":
        return "kernel"
    if command.startswith("[") and command.endswith("]"):
        return "kernel"
    if re.match(r"u\d+_a\d+", user) or re.match(r"u\d+_i\d+", user):
        return "application"
    if user in {"system", "radio", "bluetooth", "network_stack"} or lower == "system_server":
        return "android_system"
    if lower.startswith("/vendor/") or "android.hardware." in lower or "vendor." in lower:
        return "vendor_service"
    if user == "shell":
        return "tooling"
    if user == "root" or ppid in {0, 1, 2} or lower.startswith(("/system/", "/apex/")):
        return "native_system"
    return "other"


def _number_or_none(value: str) -> Optional[float]:
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _int_or_none(value: str) -> Optional[int]:
    try:
        return int(value)
    except ValueError:
        return None


def _top_helper_row(user: str, command: str, args: str = "") -> bool:
    combined = f"{command} {args}".lower()
    if "top -b -q" in combined or "top -h -b" in combined or "top -h " in combined:
        return True
    if user == "shell" and command.lower() in {"top", "sh", "shell", "shell svc"}:
        return command.lower() in {"top", "shell svc"} or "top " in combined
    return False


def parse_top_processes(text: str) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("PID ", "Tasks:", "Mem:", "Swap:", "CPU:")):
            continue
        parts = stripped.split(maxsplit=11)
        if len(parts) != 12:
            continue
        pid = _int_or_none(parts[0])
        ppid = _int_or_none(parts[2])
        cpu_pct = _number_or_none(parts[8].rstrip("%"))
        mem_pct = _number_or_none(parts[9].rstrip("%"))
        if pid is None or cpu_pct is None:
            continue
        command = parts[11]
        if _top_helper_row(parts[1], command):
            continue
        first_token = command.split(maxsplit=1)[0] if command else "unknown"
        name = first_token.rsplit("/", 1)[-1]
        descriptor = watched_activity_descriptor(command)
        activity = classify_thread_activity(name, command)
        row: Dict[str, object] = {
            "pid": pid,
            "user": parts[1],
            "ppid": ppid,
            "priority": parts[3],
            "nice": _int_or_none(parts[4]),
            "policy": parts[5],
            "resident_bytes": parse_memory_bytes(parts[6]),
            "state": parts[7],
            "cpu_pct": cpu_pct,
            "mem_pct": mem_pct,
            "cpu_time_s": parse_cpu_time_seconds(parts[10]),
            "name": name,
            "command": command,
            "category": classify_process(parts[1], command, ppid),
        }
        if descriptor:
            row.update(descriptor)
            row["activity_active"] = _activity_is_active(descriptor, cpu_pct, parts[7])
        if activity:
            row.update(activity)
            row["classified_activity_active"] = cpu_pct >= 0.5 or parts[7].upper() in {"R", "D"}
        rows.append(row)
    return sorted(rows, key=lambda item: float(item.get("cpu_pct") or 0.0), reverse=True)


def parse_top_threads(text: str) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("PID ", "Tasks:", "Mem:", "Swap:", "CPU:")):
            continue
        parts = stripped.split(maxsplit=12)
        if len(parts) != 13:
            continue
        pid = _int_or_none(parts[0])
        tid = _int_or_none(parts[1])
        cpu_pct = _number_or_none(parts[8].rstrip("%"))
        mem_pct = _number_or_none(parts[9].rstrip("%"))
        if pid is None or tid is None or cpu_pct is None:
            continue
        command = parts[11]
        args = parts[12]
        if _top_helper_row(parts[2], command, args):
            continue
        descriptor = watched_activity_descriptor(f"{command} {args}")
        activity = classify_thread_activity(command, args)
        row: Dict[str, object] = {
            "pid": pid,
            "tid": tid,
            "user": parts[2],
            "priority": parts[3],
            "nice": _int_or_none(parts[4]),
            "policy": parts[5],
            "resident_bytes": parse_memory_bytes(parts[6]),
            "state": parts[7],
            "cpu_pct": cpu_pct,
            "mem_pct": mem_pct,
            "cpu_time_s": parse_cpu_time_seconds(parts[10]),
            "name": command,
            "process": args,
            "category": classify_process(parts[2], args),
        }
        if descriptor:
            row.update(descriptor)
            row["activity_active"] = _activity_is_active(descriptor, cpu_pct, parts[7])
        if activity:
            row.update(activity)
            row["classified_activity_active"] = cpu_pct >= 0.5 or parts[7].upper() in {"R", "D"}
        rows.append(row)
    return sorted(rows, key=lambda item: float(item.get("cpu_pct") or 0.0), reverse=True)


def parse_ps_processes(text: str) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("PID "):
            continue
        parts = stripped.split(maxsplit=4)
        if len(parts) < 4:
            continue
        pid = _int_or_none(parts[0])
        ppid = _int_or_none(parts[2])
        if pid is None:
            continue
        name = parts[3]
        args = parts[4] if len(parts) > 4 else name
        descriptor = watched_activity_descriptor(f"{name} {args}")
        row: Dict[str, object] = {
            "pid": pid,
            "user": parts[1],
            "ppid": ppid,
            "name": name,
            "command": args,
            "category": classify_process(parts[1], args, ppid),
        }
        if descriptor:
            row.update(descriptor)
            row["activity_active"] = _activity_is_active(descriptor, None, None)
        rows.append(row)
    return rows


def merge_watched_processes(
    ps_processes: List[Dict[str, object]],
    top_processes: List[Dict[str, object]],
) -> List[Dict[str, object]]:
    top_by_pid = {int(item["pid"]): item for item in top_processes if isinstance(item.get("pid"), int)}
    watched: List[Dict[str, object]] = []
    for item in ps_processes:
        if not item.get("watch_kind"):
            continue
        merged = dict(item)
        top = top_by_pid.get(int(item["pid"]))
        if top:
            for key in (
                "cpu_pct",
                "mem_pct",
                "resident_bytes",
                "state",
                "policy",
                "priority",
                "nice",
                "cpu_time_s",
            ):
                merged[key] = top.get(key)
        descriptor = {key: str(value) for key, value in merged.items() if key.startswith("watch_")}
        merged["activity_active"] = _activity_is_active(
            descriptor,
            float(merged["cpu_pct"]) if isinstance(merged.get("cpu_pct"), (int, float)) else None,
            str(merged.get("state") or ""),
        )
        watched.append(merged)
    return sorted(
        watched,
        key=lambda item: (
            not bool(item.get("activity_active")),
            -float(item.get("cpu_pct") or 0.0),
            str(item.get("watch_name") or item.get("name") or ""),
        ),
    )


def _nullable_float_list(value: str) -> List[Optional[float]]:
    rows: List[Optional[float]] = []
    for token in value.split(","):
        token = token.strip()
        if not token or token.lower() in {"nan", "null", "none"}:
            rows.append(None)
            continue
        try:
            parsed = float(token)
        except ValueError:
            rows.append(None)
            continue
        rows.append(parsed if math.isfinite(parsed) else None)
    return rows


def parse_thermalservice(text: str) -> Dict[str, object]:
    status_match = re.search(r"Thermal Status:\s*(\d+)", text, re.I)
    hal_match = re.search(r"HAL Ready:\s*(true|false)", text, re.I)
    sections: Dict[str, List[str]] = {"root": []}
    current_section = "root"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith(":"):
            current_section = stripped[:-1].lower()
            sections.setdefault(current_section, [])
            continue
        sections.setdefault(current_section, []).append(line)

    def temperatures_from(lines: List[str], source: str) -> List[Dict[str, object]]:
        values: Dict[str, Dict[str, object]] = {}
        pattern = re.compile(
            r"Temperature\{mValue=([-+\d.]+),\s*mType=(\d+),\s*mName=([^,}]+),\s*mStatus=(\d+)"
        )
        for line in lines:
            match = pattern.search(line)
            if not match:
                continue
            value, type_id, name, severity = match.groups()
            values[name.strip()] = {
                "name": name.strip(),
                "value_c": float(value),
                "type": int(type_id),
                "status": int(severity),
                "source": source,
            }
        return list(values.values())

    current_lines = sections.get("current temperatures from hal", [])
    temperatures = temperatures_from(current_lines, "HAL current")
    if not temperatures:
        temperatures = temperatures_from(sections.get("cached temperatures", []), "ThermalService cache")

    cooling_devices: List[Dict[str, object]] = []
    cooling_pattern = re.compile(
        r"CoolingDevice\{mValue=([-+\d.]+),\s*mType=(\d+),\s*mName=([^,}]+)"
    )
    for line in sections.get("current cooling devices from hal", []):
        match = cooling_pattern.search(line)
        if not match:
            continue
        value, type_id, name = match.groups()
        cooling_devices.append(
            {"name": name.strip(), "value": float(value), "type": int(type_id)}
        )

    thresholds: List[Dict[str, object]] = []
    threshold_pattern = re.compile(
        r"TemperatureThreshold\{mType=(\d+),\s*mName=([^,}]+),\s*"
        r"mHotThrottlingThresholds=\[([^\]]*)\],\s*"
        r"mColdThrottlingThresholds=\[([^\]]*)\]"
    )
    for line in sections.get("temperature static thresholds from hal", []):
        match = threshold_pattern.search(line)
        if not match:
            continue
        type_id, name, hot, cold = match.groups()
        thresholds.append(
            {
                "name": name.strip(),
                "type": int(type_id),
                "hot_c": _nullable_float_list(hot),
                "cold_c": _nullable_float_list(cold),
            }
        )

    headroom: List[Optional[float]] = []
    headroom_lines = sections.get("temperature headroom thresholds", [])
    headroom_match = re.search(r"\[([^\]]*)\]", "\n".join(headroom_lines))
    if headroom_match:
        headroom = _nullable_float_list(headroom_match.group(1))
    return {
        "status": int(status_match.group(1)) if status_match else None,
        "hal_ready": hal_match.group(1).lower() == "true" if hal_match else None,
        "temperatures": temperatures,
        "cooling_devices": cooling_devices,
        "thresholds": thresholds,
        "headroom_thresholds": headroom,
    }


def parse_cpuset_policy_state(text: str) -> Dict[str, object]:
    cpusets: Dict[str, str] = {}
    policies: List[Dict[str, object]] = []
    availability: Dict[str, object] = {}
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) >= 4 and parts[0] == "CPUSET":
            name, value, status = parts[1], parts[2], parts[3]
            if status == "ok" and value:
                cpusets[name] = value
                availability[f"cpuset:{name}"] = True
            else:
                availability[f"cpuset:{name}"] = status or "unavailable"
        elif len(parts) >= 8 and parts[0] == "POLICY":
            name = parts[1]
            row = {
                "name": name,
                "governor": parts[2] or None,
                "scaling_min_khz": _number_or_none(parts[3]),
                "scaling_max_khz": _number_or_none(parts[4]),
                "cpuinfo_min_khz": _number_or_none(parts[5]),
                "cpuinfo_max_khz": _number_or_none(parts[6]),
                "status": parts[7] or "unknown",
            }
            if len(parts) >= 12:
                enabled = _int_or_none(parts[9])
                row.update(
                    {
                        "related_cpus": parts[8] or None,
                        "core_ctl_enabled": bool(enabled) if enabled is not None else None,
                        "core_ctl_min_cpus": _int_or_none(parts[10]),
                        "core_ctl_max_cpus": _int_or_none(parts[11]),
                    }
                )
            policies.append(row)
            availability[f"policy:{name}"] = row["status"]
    return {"cpusets": cpusets, "cpu_policies": policies, "availability": availability}


def parse_performance_hint(text: str) -> Dict[str, object]:
    preferred = first_number(
        re.search(r"HintSessionPreferredRate:\s*([^\n]+)", text).group(1)
        if re.search(r"HintSessionPreferredRate:\s*([^\n]+)", text)
        else None
    )
    support_match = re.search(r"Hint Session Support:\s*(true|false)", text, re.I)
    cpu_headroom = re.search(r"CPU Headroom Supported:\s*(true|false)", text, re.I)
    gpu_headroom = re.search(r"GPU Headroom Supported:\s*(true|false)", text, re.I)
    sessions: List[Dict[str, object]] = []
    current_uid: Optional[int] = None
    current: Optional[Dict[str, object]] = None
    for line in text.splitlines():
        stripped = line.strip()
        uid_match = re.fullmatch(r"Uid\s+(\d+):", stripped)
        if uid_match:
            current_uid = int(uid_match.group(1))
            current = None
            continue
        if stripped == "Session:":
            current = {"uid": current_uid}
            sessions.append(current)
            continue
        if current is None or ":" not in stripped:
            continue
        key, value = (part.strip() for part in stripped.split(":", 1))
        mapping = {
            "SessionPID": "pid",
            "SessionUID": "uid",
            "SessionTargetDurationNanos": "target_duration_ns",
            "SessionAllowedByProcState": "allowed_by_proc_state",
            "SessionForcePaused": "force_paused",
            "PowerEfficient": "power_efficient",
            "GraphicsPipeline": "graphics_pipeline",
        }
        if key == "SessionTIDs":
            current["tids"] = [int(item) for item in re.findall(r"\d+", value)]
        elif key in mapping:
            target = mapping[key]
            if value.lower() in {"true", "false"}:
                current[target] = value.lower() == "true"
            else:
                parsed = _int_or_none(value)
                current[target] = parsed if parsed is not None else value
    return {
        "preferred_rate_ns": preferred,
        "hint_session_supported": support_match.group(1).lower() == "true" if support_match else None,
        "cpu_headroom_supported": cpu_headroom.group(1).lower() == "true" if cpu_headroom else None,
        "gpu_headroom_supported": gpu_headroom.group(1).lower() == "true" if gpu_headroom else None,
        "sessions": sessions,
    }


def parse_activity_processes(
    text: str,
    include_pids: Optional[Set[int]] = None,
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    current: Optional[Dict[str, object]] = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        pid = current.get("pid")
        command = str(current.get("name") or "")
        descriptor = watched_activity_descriptor(command)
        if descriptor:
            current.update(descriptor)
        selected = include_pids is None or pid in include_pids or descriptor is not None or command == "system_server"
        if selected:
            rows.append(current)
        current = None

    header_pattern = re.compile(
        r"^\s+\*([^*]+)\*\s+UID\s+(\d+)\s+ProcessRecord\{[^}]*?\s+(\d+):(.+?)/([^}\s]+)\}"
    )
    for line in text.splitlines():
        header = header_pattern.search(line)
        if header:
            flush()
            process_class, uid, pid, name, uid_token = header.groups()
            current = {
                "process_class": process_class.strip(),
                "uid": int(uid),
                "pid": int(pid),
                "name": name.strip(),
                "uid_token": uid_token,
            }
            continue
        if current is None:
            continue
        sched = re.search(
            r"mCurSchedGroup=(-?\d+)\s+setSchedGroup=(-?\d+)\s+systemNoUi=(true|false)",
            line,
        )
        if sched:
            current.update(
                {
                    "current_sched_group": int(sched.group(1)),
                    "set_sched_group": int(sched.group(2)),
                    "system_no_ui": sched.group(3) == "true",
                }
            )
        proc_state = re.search(
            r"curProcState=(-?\d+).*?setProcState=(-?\d+).*?mAdjType=([^\s]+)",
            line,
        )
        if proc_state:
            current.update(
                {
                    "current_proc_state": int(proc_state.group(1)),
                    "set_proc_state": int(proc_state.group(2)),
                    "adj_type": proc_state.group(3),
                }
            )
        adj = re.search(r"(?:curAdj|mCurAdj)=(-?\d+)", line)
        if adj:
            current["oom_adj"] = int(adj.group(1))
        frozen = re.search(r"\bisFrozen=(true|false)", line)
        if frozen:
            current["frozen"] = frozen.group(1) == "true"
    flush()
    return rows


def parse_power_scheduler_state(text: str) -> List[str]:
    patterns = (
        "mWakefulness=",
        "mWakefulnessRaw=",
        "mIsPowered=",
        "mDeviceIdleMode=",
        "mLightDeviceIdleMode=",
        "mLowPowerModeEnabled=",
        "mBatteryLevel=",
        "mStayOn=",
        "Power HAL",
    )
    rows: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and any(pattern.lower() in stripped.lower() for pattern in patterns):
            if stripped not in rows:
                rows.append(stripped)
    return rows[:30]


def parse_thermal(text: str) -> Dict[str, object]:
    parsed = parse_thermalservice(text)
    return {
        "status": parsed.get("status"),
        "temperatures": parsed.get("temperatures", []),
    }


def parse_display(text: str, brightness_text: str, peak_refresh_text: str) -> Dict[str, object]:
    active_match = re.search(r"mActiveRenderFrameRate=([-+\d.]+)", text)
    active_refresh = first_number(active_match.group(1) if active_match else None)
    info_match = re.search(
        r'DisplayDeviceInfo\{"[^"]+":\s*uniqueId="[^"]+",\s*(\d+)\s*x\s*(\d+),\s*modeId\s*(\d+),\s*renderFrameRate\s*([-+\d.]+)',
        text,
    )
    width = height = mode_id = None
    if info_match:
        width, height, mode_id = (
            int(info_match.group(1)),
            int(info_match.group(2)),
            int(info_match.group(3)),
        )
        if active_refresh is None:
            active_refresh = float(info_match.group(4))
    return {
        "width": width,
        "height": height,
        "mode_id": mode_id,
        "active_refresh_hz": active_refresh,
        "brightness_raw": first_number(brightness_text),
        "peak_refresh_hz": first_number(peak_refresh_text),
    }


def extract_kernel_wakelocks(text: str) -> List[Dict[str, object]]:
    wakelocks: List[Dict[str, object]] = []
    pattern = re.compile(r"^\s*Kernel Wake lock\s+(.+?):\s*([^\n(]+)\s*\((\d+) times\)", re.M)
    for match in pattern.finditer(text):
        name, duration_text, count = match.groups()
        duration_s = parse_duration_seconds(duration_text)
        if duration_s is None:
            continue
        wakelocks.append({"name": name.strip(), "duration_s": duration_s, "count": int(count)})
    return sorted(wakelocks, key=lambda item: float(item["duration_s"]), reverse=True)[:20]


def parse_gpu_work(text: str) -> Dict[int, Dict[str, int]]:
    entries: Dict[int, Dict[str, int]] = {}
    in_table = False
    for line in text.splitlines():
        if "gpu_id uid total_active_duration_ns total_inactive_duration_ns" in line:
            in_table = True
            continue
        if not in_table:
            continue
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", line)
        if not match:
            if entries and line.strip():
                break
            continue
        gpu_id, uid, active_ns, inactive_ns = (int(value) for value in match.groups())
        entries[uid] = {
            "gpu_id": gpu_id,
            "active_ns": active_ns,
            "inactive_ns": inactive_ns,
        }
    return entries


def parse_gpu_dump(text: str) -> Dict[str, object]:
    global_match = re.search(r"^Global total:\s*(\d+)\s*$", text, re.M)
    memory_rows = [
        {"pid": int(pid), "bytes": int(total)}
        for pid, total in re.findall(r"^Proc\s+(\d+)\s+total:\s*(\d+)\s*$", text, re.M)
    ]
    memory_rows.sort(key=lambda item: int(item["bytes"]), reverse=True)
    stable_match = re.search(r"^Stable Game Driver:\s*(.*?)\s*$", text, re.M)
    prerelease_match = re.search(r"^Pre-release Game Driver:\s*(.*?)\s*$", text, re.M)
    return {
        "memory_available": global_match is not None or bool(memory_rows),
        "global_total_bytes": int(global_match.group(1)) if global_match else None,
        "process_memory": memory_rows,
        "stable_game_driver": stable_match.group(1).strip() if stable_match else None,
        "prerelease_game_driver": (
            prerelease_match.group(1).strip() if prerelease_match else None
        ),
    }


def format_bytes(value: Optional[int]) -> str:
    if value is None:
        return "n/a"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024.0
    return f"{value} B"


def format_duration(value_s: Optional[float]) -> str:
    if value_s is None:
        return "n/a"
    seconds = max(0, int(round(value_s)))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"
