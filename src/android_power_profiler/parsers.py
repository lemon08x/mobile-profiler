from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple


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


def parse_thermal(text: str) -> Dict[str, object]:
    status_match = re.search(r"Thermal Status:\s*(\d+)", text, re.I)
    temperatures: Dict[str, Dict[str, object]] = {}
    pattern = re.compile(
        r"Temperature\{mValue=([-+\d.]+),\s*mType=(\d+),\s*mName=([^,}]+),\s*mStatus=(\d+)"
    )
    for match in pattern.finditer(text):
        value, type_id, name, status = match.groups()
        temperatures[name.strip()] = {
            "name": name.strip(),
            "value_c": float(value),
            "type": int(type_id),
            "status": int(status),
        }
    return {
        "status": int(status_match.group(1)) if status_match else None,
        "temperatures": list(temperatures.values()),
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
