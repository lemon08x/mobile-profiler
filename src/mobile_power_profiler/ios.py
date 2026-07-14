from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import run as _run_subprocess
from typing import Dict, List, Optional, Sequence, Tuple

from .models import ClockSyncPoint, ContextSample, SystemSnapshot, ThermalSnapshot
from .storage import RunJournal


DEFAULT_IOS_PYTHON = os.environ.get("IOS_PYTHON", sys.executable)
IOS_DEVICE_PREFIX = "ios:"
LEGACY_ANDROID_IOS_STATE_DIR = Path(
    os.environ.get(
        "ANDROID_POWER_PROFILER_STATE_DIR",
        Path.home() / ".android-power-profiler",
    )
).expanduser()
LEGACY_MOBILE_POWER_IOS_STATE_DIR = Path(
    os.environ.get(
        "MOBILE_POWER_PROFILER_STATE_DIR",
        Path.home() / ".mobile-power-profiler",
    )
).expanduser()
IOS_STATE_DIR = Path(
    os.environ.get(
        "MOBILE_PROFILER_STATE_DIR",
        os.environ.get(
            "MOBILE_POWER_PROFILER_STATE_DIR",
            os.environ.get(
                "ANDROID_POWER_PROFILER_STATE_DIR",
                Path.home() / ".mobile-profiler",
            ),
        ),
    )
).expanduser()
IOS_ENDPOINTS_PATH = IOS_STATE_DIR / "ios-devices.json"
LEGACY_ANDROID_IOS_ENDPOINTS_PATH = LEGACY_ANDROID_IOS_STATE_DIR / "ios-devices.json"
LEGACY_MOBILE_POWER_IOS_ENDPOINTS_PATH = LEGACY_MOBILE_POWER_IOS_STATE_DIR / "ios-devices.json"
# Compatibility alias retained for callers that used the original constant.
LEGACY_IOS_ENDPOINTS_PATH = LEGACY_ANDROID_IOS_ENDPOINTS_PATH


@dataclass
class IOSCollectionResult:
    sample_count: int = 0
    context_count: int = 0
    system_snapshot_count: int = 0
    thermal_snapshot_count: int = 0
    clock_sync_count: int = 0
    reconnect_count: int = 0
    sampler_launch_count: int = 0
    host_elapsed_s: float = 0.0
    stop_reason: str = "completed"
    warnings: List[str] = field(default_factory=list)
    battery_end: Dict[str, object] = field(default_factory=dict)
    stats: Dict[str, object] = field(default_factory=dict)
    last_device_uptime_s: Optional[float] = None


def ios_bridge_path() -> Path:
    return Path(__file__).with_name("ios_bridge.py")


def ios_udid(value: object) -> str:
    text = str(value or "").strip()
    return text[len(IOS_DEVICE_PREFIX) :] if text.lower().startswith(IOS_DEVICE_PREFIX) else text


def ios_device_id(udid: str) -> str:
    return f"{IOS_DEVICE_PREFIX}{udid}"


def _bridge_command(ios_python: str, *arguments: object) -> List[str]:
    return [str(ios_python), str(ios_bridge_path()), *(str(value) for value in arguments)]


def _run_bridge_json(
    ios_python: str,
    arguments: Sequence[object],
    *,
    timeout_s: float,
) -> Dict[str, object]:
    try:
        # Keep the optional sidecar launcher isolated from callers that mock the
        # main UI/ADB subprocess module.  This also makes an iOS discovery
        # failure non-fatal to the Android-only workflow.
        result = _run_subprocess(
            _bridge_command(ios_python, *arguments),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except (OSError, TypeError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"iOS sidecar could not start: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
            or f"iOS sidecar exited with code {result.returncode}"
        )
    for line in reversed(result.stdout.splitlines()):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError("iOS sidecar returned no JSON object")


def _read_endpoints(path: Path) -> Dict[str, Dict[str, object]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(key): dict(item)
        for key, item in value.items()
        if isinstance(item, dict)
    }


def _load_endpoints() -> Dict[str, Dict[str, object]]:
    endpoints: Dict[str, Dict[str, object]] = {}
    for path in dict.fromkeys(
        (
            LEGACY_IOS_ENDPOINTS_PATH,
            LEGACY_MOBILE_POWER_IOS_ENDPOINTS_PATH,
            IOS_ENDPOINTS_PATH,
        )
    ):
        endpoints.update(_read_endpoints(path))
    return endpoints


def _write_endpoints(value: Dict[str, Dict[str, object]]) -> None:
    IOS_ENDPOINTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = IOS_ENDPOINTS_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(IOS_ENDPOINTS_PATH)


def _save_endpoint(
    udid: str,
    host: object,
    port: object,
    device: Optional[Dict[str, object]] = None,
) -> None:
    if not host or not isinstance(port, (int, float)):
        return
    endpoints = _load_endpoints()
    current = dict(endpoints.get(udid, {}))
    current.update(
        {
            "host": str(host),
            "port": int(port),
            "updated_at": time.time(),
        }
    )
    if device:
        for key in ("model", "name", "product_type", "product_version", "build_version"):
            if device.get(key):
                current[key] = device[key]
    endpoints[udid] = current
    _write_endpoints(endpoints)


def _endpoint_reachable(host: object, port: object, timeout_s: float = 0.5) -> bool:
    if not host or not isinstance(port, (int, float)):
        return False
    try:
        with socket.create_connection((str(host), int(port)), timeout=timeout_s):
            return True
    except OSError:
        return False


def list_ios_devices(
    ios_python: str = DEFAULT_IOS_PYTHON,
) -> Tuple[List[Dict[str, str]], Optional[str]]:
    bridge_error: Optional[str] = None
    try:
        payload = _run_bridge_json(ios_python, ["list"], timeout_s=20.0)
    except RuntimeError as exc:
        payload = {}
        bridge_error = str(exc)
    raw_devices = payload.get("devices")
    raw_devices = raw_devices if isinstance(raw_devices, list) else []
    endpoints = _load_endpoints()
    devices: Dict[str, Dict[str, str]] = {}
    for raw in raw_devices:
        if not isinstance(raw, dict) or not raw.get("udid"):
            continue
        udid = str(raw["udid"])
        cached = dict(endpoints.get(udid, {}))
        host = raw.get("host") or raw.get("wireless_host") or cached.get("host")
        port = raw.get("port") or raw.get("wireless_port") or cached.get("port")
        if host and isinstance(port, (int, float)):
            _save_endpoint(udid, host, port, raw)
        remote_ready = _endpoint_reachable(host, port)
        state = str(raw.get("state") or "offline")
        if remote_ready:
            state = "device"
        reported_connection = str(raw.get("connection_type") or "usb").lower()
        usb_present = reported_connection == "usb"
        connection_type = (
            "usb"
            if usb_present
            else "wireless"
            if remote_ready
            else reported_connection
        )
        transports = ["usb"] if usb_present else []
        if remote_ready:
            transports.append("wireless")
        devices[udid] = {
            "serial": ios_device_id(udid),
            "udid": udid,
            "state": state,
            "platform": "ios",
            "connection_type": connection_type,
            "transports": ",".join(transports),
            "wireless_ready": str(remote_ready).lower(),
            "model": str(raw.get("name") or cached.get("model") or "iPhone"),
            "product": str(raw.get("product_type") or cached.get("product_type") or "iOS"),
            "product_version": str(
                raw.get("product_version") or cached.get("product_version") or ""
            ),
            "host": str(host or ""),
            "port": str(int(port)) if isinstance(port, (int, float)) else "",
            "remote_paired": str(bool(raw.get("remote_paired") or cached)).lower(),
        }

    for udid, cached in endpoints.items():
        if udid in devices:
            continue
        host = cached.get("host")
        port = cached.get("port")
        state = "device" if _endpoint_reachable(host, port) else "offline"
        devices[udid] = {
            "serial": ios_device_id(udid),
            "udid": udid,
            "state": state,
            "platform": "ios",
            "connection_type": "wireless",
            "transports": "wireless",
            "wireless_ready": str(state == "device").lower(),
            "model": str(cached.get("model") or cached.get("name") or "iPhone"),
            "product": str(cached.get("product_type") or "iOS"),
            "product_version": str(cached.get("product_version") or ""),
            "host": str(host or ""),
            "port": str(port or ""),
            "remote_paired": "true",
        }
    return list(devices.values()), bridge_error


def select_ios_device(
    requested: Optional[str],
    ios_python: str = DEFAULT_IOS_PYTHON,
) -> Dict[str, str]:
    devices, error = list_ios_devices(ios_python)
    ready = [item for item in devices if item.get("state") == "device"]
    if error and not ready:
        raise RuntimeError(error)
    if requested:
        requested_id = ios_device_id(ios_udid(requested))
        selected = next((item for item in ready if item.get("serial") == requested_id), None)
        if selected is None:
            connected = ", ".join(item.get("serial", "") for item in ready) or "none"
            raise RuntimeError(
                f"iOS device {requested_id!r} is not ready; connected devices: {connected}"
            )
        return selected
    if not ready:
        raise RuntimeError("no paired iPhone is available over USB or Wi-Fi")
    if len(ready) > 1:
        connected = ", ".join(item.get("serial", "") for item in ready)
        raise RuntimeError(f"multiple iPhones are available; pass --device. Devices: {connected}")
    return ready[0]


def pair_ios_device(
    requested: Optional[str],
    ios_python: str = DEFAULT_IOS_PYTHON,
    timeout_s: float = 12.0,
) -> Dict[str, object]:
    arguments: List[object] = ["pair", "--timeout", timeout_s]
    if requested:
        arguments.extend(["--udid", ios_udid(requested)])
    result = _run_bridge_json(ios_python, arguments, timeout_s=max(60.0, timeout_s + 20.0))
    udid = str(result.get("udid") or ios_udid(requested))
    endpoint = result.get("endpoint")
    endpoint = endpoint if isinstance(endpoint, dict) else {}
    device = result.get("device")
    device = device if isinstance(device, dict) else {}
    _save_endpoint(udid, endpoint.get("host"), endpoint.get("port"), device)
    result["serial"] = ios_device_id(udid)
    return result


def _device_endpoint(device: Dict[str, str]) -> tuple[Optional[str], Optional[int]]:
    host = str(device.get("host") or "").strip() or None
    try:
        port = int(device.get("port") or 0) or None
    except ValueError:
        port = None
    return host, port


def probe_ios_device(
    requested: Optional[str],
    ios_python: str = DEFAULT_IOS_PYTHON,
) -> Dict[str, object]:
    device = select_ios_device(requested, ios_python)
    udid = str(device["udid"])
    host, port = _device_endpoint(device)
    arguments: List[object] = ["probe", "--udid", udid]
    if host and port:
        arguments.extend(["--host", host, "--port", port])
    result = _run_bridge_json(ios_python, arguments, timeout_s=90.0)
    result["selected_device"] = device
    if host and port:
        _save_endpoint(udid, host, port, result.get("device") if isinstance(result.get("device"), dict) else None)
    return result


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
            process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[arg-type]
        else:
            process.send_signal(signal.SIGINT)
        process.wait(timeout=8)
        return
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    with suppress(OSError, ValueError, subprocess.TimeoutExpired):
        process.terminate()
        process.wait(timeout=3)
        return
    with suppress(OSError, ValueError, subprocess.TimeoutExpired):
        process.kill()


def collect_ios_session(
    ios_python: str,
    device: Dict[str, str],
    duration_s: int,
    interval_s: float,
    journal: RunJournal,
    *,
    checkpoint_interval_s: float,
    reconnect_timeout_s: float,
    system_monitor_enabled: bool,
    process_interval_s: float,
) -> IOSCollectionResult:
    udid = str(device["udid"])
    host, port = _device_endpoint(device)
    if not host or not port:
        raise RuntimeError(
            "iOS recording requires a cached RemotePairing Wi-Fi endpoint; run ios-pair while USB is connected"
        )

    result = IOSCollectionResult()
    started = time.monotonic()
    deadline = started + float(duration_s)
    outage_started: Optional[float] = None
    last_checkpoint = started
    stderr_lines: List[str] = []
    end_stats: List[Dict[str, object]] = []

    def checkpoint(status: str) -> None:
        journal.checkpoint(
            {
                "status": status,
                "sample_count": result.sample_count,
                "context_count": result.context_count,
                "clock_sync_count": result.clock_sync_count,
                "system_snapshot_count": result.system_snapshot_count,
                "thermal_snapshot_count": result.thermal_snapshot_count,
                "scheduler_snapshot_count": 0,
                "last_device_uptime_s": result.last_device_uptime_s,
                "reconnect_count": result.reconnect_count,
                "sampler_launch_count": result.sampler_launch_count,
                "stop_reason": result.stop_reason,
            }
        )

    while time.monotonic() < deadline:
        samples_before_launch = result.sample_count
        remaining = max(2.0, deadline - time.monotonic())
        command: List[str] = _bridge_command(
            ios_python,
            "record",
            "--udid",
            udid,
            "--host",
            host,
            "--port",
            port,
            "--duration",
            remaining,
            "--interval",
            interval_s,
            "--process-interval",
            process_interval_s,
            "--clock-interval",
            checkpoint_interval_s,
        )
        if not system_monitor_enabled:
            command.append("--no-system-monitor")
        creationflags = 0
        if os.name == "nt" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        except OSError as exc:
            raise RuntimeError(f"could not start iOS sidecar: {exc}") from exc
        result.sampler_launch_count += 1

        def drain_stderr() -> None:
            if process.stderr is None:
                return
            for line in process.stderr:
                text = line.rstrip("\r\n")
                if not text:
                    continue
                stderr_lines.append(text)
                journal.append_stderr_line(text)

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()
        try:
            if process.stdout is None:
                raise RuntimeError("iOS sidecar stdout was not created")
            for line in process.stdout:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    journal.append_stderr_line(f"invalid iOS sidecar JSON: {line.rstrip()}")
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                if event_type == "sample" and isinstance(event.get("sample"), dict):
                    sample = dict(event["sample"])
                    journal.append_sampler_line(
                        "N|" + json.dumps(sample, ensure_ascii=False, separators=(",", ":"))
                    )
                    result.sample_count += 1
                    if isinstance(sample.get("uptime_s"), (int, float)):
                        result.last_device_uptime_s = float(sample["uptime_s"])
                elif event_type == "context" and isinstance(event.get("context"), dict):
                    journal.append_context(ContextSample(**event["context"]))
                    result.context_count += 1
                elif event_type == "clock" and isinstance(event.get("clock"), dict):
                    journal.append_clock_sync(ClockSyncPoint(**event["clock"]))
                    result.clock_sync_count += 1
                elif event_type == "ready":
                    clock = event.get("clock")
                    if isinstance(clock, dict):
                        journal.append_clock_sync(ClockSyncPoint(**clock))
                        result.clock_sync_count += 1
                elif event_type == "system" and isinstance(event.get("snapshot"), dict):
                    journal.append_system_snapshot(SystemSnapshot(**event["snapshot"]))
                    result.system_snapshot_count += 1
                elif event_type == "thermal" and isinstance(event.get("snapshot"), dict):
                    journal.append_thermal_snapshot(ThermalSnapshot(**event["snapshot"]))
                    result.thermal_snapshot_count += 1
                elif event_type == "warning" and event.get("message"):
                    message = str(event["message"])
                    if message not in result.warnings:
                        result.warnings.append(message)
                    journal.append_stderr_line(message)
                elif event_type == "end":
                    if isinstance(event.get("battery"), dict):
                        result.battery_end = dict(event["battery"])
                    if isinstance(event.get("stats"), dict):
                        end_stats.append(dict(event["stats"]))

                now = time.monotonic()
                if now - last_checkpoint >= checkpoint_interval_s:
                    checkpoint("collecting")
                    last_checkpoint = now
        except KeyboardInterrupt:
            result.stop_reason = "interrupted"
            _stop_process(process)
            raise
        finally:
            if process.poll() is None and time.monotonic() >= deadline:
                _stop_process(process)
            returncode = process.wait()
            stderr_thread.join(timeout=2)

        if result.sample_count > samples_before_launch:
            outage_started = None

        if returncode == 0:
            result.stop_reason = "completed"
            break

        if time.monotonic() >= deadline:
            result.stop_reason = "completed" if result.sample_count >= 2 else "collector_error"
            break
        if outage_started is None:
            outage_started = time.monotonic()
        if time.monotonic() - outage_started >= reconnect_timeout_s:
            result.stop_reason = "ios_disconnected"
            result.warnings.append(
                "iPhone Wi-Fi RemotePairing did not recover before the reconnect timeout."
            )
            break
        result.reconnect_count += 1
        result.stop_reason = "ios_disconnected"
        checkpoint("reconnecting")
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))

    result.host_elapsed_s = time.monotonic() - started
    if end_stats:
        result.stats = dict(end_stats[-1])
        counts = [
            int(item.get("sample_count") or 0)
            for item in end_stats
            if isinstance(item.get("sample_count"), (int, float))
        ]
        weighted_overhead = [
            (float(item["average_collector_cpu_pct"]), int(item.get("sample_count") or 0))
            for item in end_stats
            if isinstance(item.get("average_collector_cpu_pct"), (int, float))
        ]
        result.stats["sample_count"] = sum(counts) if counts else result.sample_count
        total_weight = sum(weight for _, weight in weighted_overhead)
        if total_weight > 0:
            result.stats["average_collector_cpu_pct"] = sum(
                value * weight for value, weight in weighted_overhead
            ) / total_weight
        result.stats["sidecar_session_count"] = len(end_stats)
    if stderr_lines:
        journal.write_raw_output("ios_sidecar_stderr", "\n".join(stderr_lines))
    checkpoint("collected" if result.sample_count >= 2 else "failed")
    return result
