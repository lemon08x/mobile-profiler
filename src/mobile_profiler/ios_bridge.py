from __future__ import annotations

"""Optional pymobiledevice3 sidecar used by the platform-neutral collector.

This file is intentionally executable by a Python interpreter that does not have
the main project installed.  The parent process talks to it through JSON/JSONL,
keeping the ADB standard-library runtime independent from the optional iOS
dependency and providing the same boundary a native HarmonyOS sidecar can use.
"""

import argparse
import asyncio
import dataclasses
import inspect
import importlib.metadata
import json
import math
import os
import plistlib
import socket
import ssl
import sys
import time
import warnings
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, AsyncIterator, Optional


_WINDOWS_DLL_DIRECTORY_HANDLES: list[object] = []


def _configure_windows_sslpsk_runtime() -> list[str]:
    """Expose the OpenSSL runtime required by sslpsk-pmd3 on Windows.

    A venv created from Conda can execute without inheriting Conda's
    ``Library\\bin`` DLL search path.  Some sslpsk-pmd3 wheels still link to
    OpenSSL 1.1, so pymobiledevice3 otherwise silently leaves
    ``SSLPSKContext`` as ``None`` and RemotePairing fails much later with an
    opaque ``NoneType is not callable`` error.
    """

    add_directory = getattr(os, "add_dll_directory", None)
    if sys.platform != "win32" or not callable(add_directory):
        return []

    bases: list[Path] = []
    for value in (
        getattr(sys, "prefix", None),
        getattr(sys, "base_prefix", None),
        os.environ.get("CONDA_PREFIX"),
    ):
        if value:
            path = Path(str(value))
            if path not in bases:
                bases.append(path)

    candidates: list[Path] = []
    for base in bases:
        candidates.append(base / "Library" / "bin")
        package_root = base / "pkgs"
        if package_root.is_dir():
            candidates.extend(
                sorted(
                    package_root.glob("openssl-*/Library/bin"),
                    key=lambda item: item.parent.parent.parent.name,
                    reverse=True,
                )
            )
    for value in os.environ.get("PATH", "").split(os.pathsep):
        if value:
            candidates.append(Path(value))

    configured: list[str] = []
    seen: set[str] = set()
    required = ("libssl-1_1-x64.dll", "libcrypto-1_1-x64.dll")
    for directory in candidates:
        key = os.path.normcase(os.path.abspath(str(directory)))
        if key in seen or not all((directory / name).is_file() for name in required):
            continue
        seen.add(key)
        try:
            handle = add_directory(str(directory))
        except OSError:
            continue
        _WINDOWS_DLL_DIRECTORY_HANDLES.append(handle)
        configured.append(str(directory))
    return configured


def _configure_windows_event_loop() -> None:
    if sys.platform != "win32":
        return
    policy_factory = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy_factory is None:
        return
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=DeprecationWarning)
        asyncio.set_event_loop_policy(policy_factory())


_configure_windows_sslpsk_runtime()
_configure_windows_event_loop()


def _pairing_tls_runtime_error(
    tunnel_service_module: object,
    *,
    python_version: Optional[tuple[int, ...]] = None,
    ssl_context_type: Optional[type[ssl.SSLContext]] = None,
) -> Optional[str]:
    """Return an actionable error when the selected Python cannot create the TCP PSK tunnel."""

    version = tuple(python_version or sys.version_info[:3])
    context_type = ssl_context_type or ssl.SSLContext
    if version >= (3, 13):
        if callable(getattr(context_type, "set_psk_client_callback", None)):
            return None
        return (
            "Python 3.13+ is present, but its SSLContext does not expose the native "
            "PSK callback required by pymobiledevice3 TCP tunnels; use the official "
            "CPython 3.13+ Windows build"
        )
    if getattr(tunnel_service_module, "SSLPSKContext", None) is None:
        return (
            "Python below 3.13 requires sslpsk-pmd3 and a compatible OpenSSL 1.1 "
            "runtime. iOS 18.2+ removed QUIC tunnel support, so use an official "
            "CPython 3.13+ iOS sidecar instead"
        )
    return None


def _userspace_tunnel_runtime_error(userspace_tunnel_module: object) -> Optional[str]:
    """Detect the incompatible asynchronous PyTCP API selected by an unbounded pip resolve."""

    stack_module = getattr(userspace_tunnel_module, "stack", None)
    if inspect.iscoroutinefunction(getattr(stack_module, "start", None)):
        return (
            "pymobiledevice3 9.34.0 expects the synchronous pmd-pytcp API, but "
            "pmd-pytcp 0.1.0+ exposes asynchronous stack/socket methods. Install "
            "pmd-pytcp==0.0.6 in the iOS sidecar environment"
        )
    return None


DISCOVERY_IMPORT_ERROR: Optional[BaseException] = None
PAIRING_IMPORT_ERROR: Optional[BaseException] = None
COLLECTION_IMPORT_ERROR: Optional[BaseException] = None
ApplicationListing = None
try:
    from pymobiledevice3 import usbmux
    from pymobiledevice3.lockdown import (
        create_using_tcp,
        create_using_usbmux,
    )
    try:
        from pymobiledevice3.lockdown import get_mobdev2_devices
    except ImportError:
        get_mobdev2_devices = None
    try:
        from pymobiledevice3.lockdown import get_mobdev2_lockdowns
    except ImportError:
        get_mobdev2_lockdowns = None
    from pymobiledevice3.pair_records import get_remote_pairing_record_filename
except BaseException as exc:  # pragma: no cover - exercised through the parent runtime check
    DISCOVERY_IMPORT_ERROR = exc

try:
    from pymobiledevice3.bonjour import browse_remotepairing
    from pymobiledevice3.exceptions import RemotePairingCompletedError
    from pymobiledevice3.remote import tunnel_service, userspace_tunnel
    from pymobiledevice3.remote.tunnel_service import (
        CoreDeviceTunnelProxy,
        RemotePairingLockdownService,
        create_core_device_tunnel_service_using_remotepairing,
    )
    pairing_tls_error = _pairing_tls_runtime_error(tunnel_service)
    if pairing_tls_error is not None:
        raise RuntimeError(pairing_tls_error)
    userspace_tunnel_error = _userspace_tunnel_runtime_error(userspace_tunnel)
    if userspace_tunnel_error is not None:
        raise RuntimeError(userspace_tunnel_error)
except BaseException as exc:  # pragma: no cover - exercised through pairing/probe checks
    PAIRING_IMPORT_ERROR = exc

try:
    from pymobiledevice3.services.diagnostics import DiagnosticsService
    from pymobiledevice3.services.notification_proxy import NotificationProxyService
    try:
        from pymobiledevice3.services.dvt.instruments.application_listing import ApplicationListing
    except ImportError:
        ApplicationListing = None
    from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.graphics import Graphics
    from pymobiledevice3.services.dvt.instruments.notifications import Notifications
    from pymobiledevice3.services.dvt.instruments.sysmontap import Sysmontap
except BaseException as exc:  # pragma: no cover - exercised through probe/record checks
    COLLECTION_IMPORT_ERROR = exc


DEFAULT_REMOTE_PORT = 49152
PAIR_CACHE = Path.home() / ".pymobiledevice3"
COLLECTOR_PROCESSES = {"sysmond", "DTServiceHub", "remotepairingdeviced"}
GPU_SAMPLE_STALE_AFTER_S = 5.0
SYSTEM_LOAD_SAMPLE_STALE_AFTER_S = 30.0


def _ios_collection_target_coverage_s(duration_s: float, interval_s: float) -> float:
    """Choose the longest configured sample span that does not exceed the request."""

    duration = float(duration_s)
    if duration <= 0:
        return math.inf
    interval = max(0.001, float(interval_s))
    completed_intervals = max(1, int(math.floor((duration + 1e-9) / interval)))
    return completed_intervals * interval


def _ios_collection_window_complete(
    first_uptime_s: float,
    current_uptime_s: float,
    duration_s: float,
    interval_s: float,
) -> bool:
    if float(duration_s) <= 0:
        return False
    target = _ios_collection_target_coverage_s(duration_s, interval_s)
    tolerance = min(0.5, max(0.05, float(interval_s) * 0.1))
    return float(current_uptime_s) - float(first_uptime_s) >= target - tolerance


def _json_default(value: object) -> object:
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()  # type: ignore[union-attr]
        except Exception:
            pass
    return str(value)


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, default=_json_default), flush=True)


def _emit(event_type: str, **payload: object) -> None:
    _print_json({"type": event_type, **payload})


def _require_discovery_runtime() -> None:
    if DISCOVERY_IMPORT_ERROR is not None:
        raise RuntimeError(
            "pymobiledevice3 device discovery is unavailable in the selected iOS "
            f"Python runtime: {DISCOVERY_IMPORT_ERROR}"
        )


def _require_pairing_runtime() -> None:
    _require_discovery_runtime()
    if PAIRING_IMPORT_ERROR is not None:
        raise RuntimeError(
            "pymobiledevice3 RemotePairing support is unavailable in the selected "
            f"iOS Python runtime: {PAIRING_IMPORT_ERROR}"
        )


def _require_collection_runtime() -> None:
    _require_pairing_runtime()
    if COLLECTION_IMPORT_ERROR is not None:
        raise RuntimeError(
            "pymobiledevice3 DVT collection support is unavailable in the selected "
            f"iOS Python runtime: {COLLECTION_IMPORT_ERROR}"
        )


def _require_tunnel_runtime() -> None:
    try:
        version = importlib.metadata.version("pmd-pytcp")
    except importlib.metadata.PackageNotFoundError:
        return
    if version.startswith("0.1."):
        raise RuntimeError(
            "pymobiledevice3 9.34.0 is incompatible with pmd-pytcp "
            f"{version}; install pmd-pytcp==0.0.6 in the selected iOS Python runtime"
        )


def _pair_record_candidates(udid: str) -> list[Path]:
    candidates = [PAIR_CACHE / f"{udid}.plist"]
    if os.name == "nt":
        program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        lockdown = program_data / "Apple" / "Lockdown"
        candidates.extend(
            [
                lockdown / f"{udid}.plist.tmp",
                lockdown / f"{udid}.plist",
            ]
        )
    return candidates


def _load_pair_record(path: Path) -> Optional[dict[str, object]]:
    try:
        value = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return value if isinstance(value, dict) else None


def _remote_pair_record_path(udid: str) -> Path:
    return PAIR_CACHE / f"{get_remote_pairing_record_filename(udid)}.plist"


async def _usb_devices() -> list[object]:
    return [device for device in await usbmux.list_devices() if device.connection_type == "USB"]


async def _open_usb_lockdown(udid: str, *, autopair: bool) -> object:
    for path in _pair_record_candidates(udid):
        record = _load_pair_record(path)
        if record is None:
            continue
        client = await create_using_usbmux(
            serial=udid,
            pair_record=record,
            autopair=False,
        )
        if client.paired:
            return client
        await client.close()

    if not autopair:
        raise RuntimeError(
            "the iPhone is visible over USB but has no valid trust record; unlock it and trust this computer"
        )

    try:
        return await create_using_usbmux(serial=udid, autopair=True)
    except Exception:
        # Apple Devices can accept the trust operation but reject overwriting its
        # stale pair record with Win32 error 183. pymobiledevice3 has already saved
        # the fresh record in its local cache, so retry it explicitly.
        record = _load_pair_record(PAIR_CACHE / f"{udid}.plist")
        if record is None:
            raise
        client = await create_using_usbmux(
            serial=udid,
            pair_record=record,
            autopair=False,
        )
        if not client.paired:
            await client.close()
            raise
        return client


async def _open_tcp_lockdown(udid: str, host: str) -> object:
    for path in _pair_record_candidates(udid):
        record = _load_pair_record(path)
        if record is None:
            continue
        client = await create_using_tcp(hostname=host, pair_record=record, autopair=False)
        if client.paired:
            return client
        await client.close()
    raise RuntimeError(f"no valid Wi-Fi lockdown pair record for {udid}")


def _peer_host(client: object) -> Optional[str]:
    hostname = getattr(client, "hostname", None)
    if hostname:
        return str(hostname).split("%", 1)[0]
    service = getattr(client, "service", None)
    writer = getattr(service, "writer", None)
    if writer is not None:
        peer = writer.get_extra_info("peername")
        if isinstance(peer, tuple) and peer:
            return str(peer[0]).split("%", 1)[0]
    return None


def _tcp_reachable(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _private_ipv4(address: object) -> bool:
    text = str(address)
    return text.startswith("10.") or text.startswith("192.168.") or any(
        text.startswith(f"172.{part}.") for part in range(16, 32)
    )


async def _network_devices() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    clients: list[tuple[Optional[str], object]] = []
    if get_mobdev2_devices is not None:
        clients = [(None, client) for client in list(await get_mobdev2_devices())]
    elif get_mobdev2_lockdowns is not None:
        clients = [
            (host, client)
            async for host, client in get_mobdev2_lockdowns(
                only_paired=False,
                timeout=2.0,
            )
        ]
    for discovered_host, client in clients:
        try:
            values = dict(getattr(client, "all_values", {}) or {})
            udid = str(
                values.get("UniqueDeviceID")
                or getattr(client, "udid", "")
                or getattr(client, "identifier", "")
            )
            host = str(discovered_host).split("%", 1)[0] if discovered_host else _peer_host(client)
            if not udid or not host:
                continue
            remote_paired = _remote_pair_record_path(udid).is_file()
            remote_port = DEFAULT_REMOTE_PORT if remote_paired and _tcp_reachable(host, DEFAULT_REMOTE_PORT) else None
            rows.append(
                {
                    "udid": udid,
                    "name": values.get("DeviceName") or "iPhone",
                    "product_type": values.get("ProductType"),
                    "product_version": values.get("ProductVersion"),
                    "build_version": values.get("BuildVersion"),
                    "connection_type": "wireless",
                    "host": host,
                    "port": remote_port,
                    "remote_paired": remote_paired,
                    "state": "device" if remote_port else "basic",
                }
            )
        finally:
            with suppress(Exception):
                await client.close()
    return rows


async def list_devices() -> dict[str, object]:
    _require_discovery_runtime()
    rows: dict[str, dict[str, object]] = {}
    discovery_warnings: list[str] = []
    try:
        usb_devices = await _usb_devices()
    except Exception as exc:
        usb_devices = []
        discovery_warnings.append(f"USB discovery unavailable: {exc}")
    for device in usb_devices:
        udid = str(device.serial)
        item: dict[str, object] = {
            "udid": udid,
            "name": "iPhone",
            "product_type": None,
            "product_version": None,
            "build_version": None,
            "connection_type": "usb",
            "remote_paired": _remote_pair_record_path(udid).is_file(),
            "state": "unauthorized",
        }
        try:
            client = await _open_usb_lockdown(udid, autopair=False)
        except Exception:
            rows[udid] = item
            continue
        try:
            values = dict(client.all_values or {})
            item.update(
                {
                    "name": values.get("DeviceName") or "iPhone",
                    "product_type": values.get("ProductType"),
                    "product_version": values.get("ProductVersion"),
                    "build_version": values.get("BuildVersion"),
                    "state": "device",
                }
            )
        finally:
            await client.close()
        rows[udid] = item

    try:
        network_devices = await _network_devices()
    except Exception as exc:
        network_devices = []
        discovery_warnings.append(f"network discovery unavailable: {exc}")
    for item in network_devices:
        udid = str(item["udid"])
        existing = rows.get(udid)
        if existing is None or existing.get("state") != "device":
            rows[udid] = item
        elif item.get("port"):
            existing["wireless_host"] = item.get("host")
            existing["wireless_port"] = item.get("port")
            existing["remote_paired"] = item.get("remote_paired")

    return {"devices": list(rows.values()), "warnings": discovery_warnings}


async def _find_remote_endpoint(udid: str, timeout: float) -> Optional[dict[str, object]]:
    answers = await browse_remotepairing(timeout=timeout)
    candidates: list[tuple[str, int]] = []
    for answer in answers:
        addresses = list(getattr(answer, "addresses", []) or [])
        addresses.sort(key=lambda item: not _private_ipv4(getattr(item, "ip", item)))
        for address in addresses:
            host = str(getattr(address, "full_ip", None) or getattr(address, "ip", address))
            candidates.append((host, int(answer.port)))

    for host, port in candidates:
        service = None
        try:
            service = await create_core_device_tunnel_service_using_remotepairing(
                udid,
                host,
                port,
                autopair=False,
            )
            return {"host": host.split("%", 1)[0], "port": port}
        except Exception:
            continue
        finally:
            if service is not None:
                with suppress(Exception):
                    await service.close()

    for item in await _network_devices():
        if item.get("udid") == udid and item.get("host") and item.get("port"):
            return {"host": item["host"], "port": item["port"]}
    return None


async def pair_device(udid: Optional[str], timeout: float) -> dict[str, object]:
    _require_pairing_runtime()
    devices = await _usb_devices()
    if udid:
        devices = [device for device in devices if str(device.serial) == udid]
    if len(devices) != 1:
        raise RuntimeError(f"expected one matching USB iPhone, found {len(devices)}")
    udid = str(devices[0].serial)
    client = await _open_usb_lockdown(udid, autopair=True)
    created = False
    try:
        service = await RemotePairingLockdownService.create(client)
        try:
            try:
                await service.connect(autopair=True)
            except RemotePairingCompletedError:
                created = True
        finally:
            await service.close()
    finally:
        await client.close()

    if created:
        await asyncio.sleep(0.8)
    client = await _open_usb_lockdown(udid, autopair=False)
    try:
        service = await RemotePairingLockdownService.create(client)
        try:
            await service.connect(autopair=False)
            if service.encryption_key is None:
                raise RuntimeError("RemotePairing verification failed")
        finally:
            await service.close()
    finally:
        await client.close()

    endpoint = await _find_remote_endpoint(udid, timeout)
    values = await _device_values(udid, None)
    return {
        "udid": udid,
        "created": created,
        "remote_pairing": True,
        "endpoint": endpoint,
        "device": values,
    }


async def _device_values(udid: str, host: Optional[str]) -> dict[str, object]:
    client = await (_open_tcp_lockdown(udid, host) if host else _open_usb_lockdown(udid, autopair=False))
    try:
        values = dict(client.all_values or {})
        try:
            developer_mode = await client.get_developer_mode_status()
        except Exception:
            developer_mode = None
        try:
            wifi_connections = await client.get_enable_wifi_connections()
        except Exception:
            wifi_connections = None
        return {
            "serial": udid,
            "brand": "Apple",
            "model": values.get("DeviceName") or values.get("ProductType") or "iPhone",
            "device": values.get("ProductType"),
            "product_type": values.get("ProductType"),
            "hardware": values.get("HardwareModel"),
            "cpu_architecture": values.get("CPUArchitecture"),
            "ios": values.get("ProductVersion"),
            "build": values.get("BuildVersion"),
            "device_class": values.get("DeviceClass"),
            "developer_mode": developer_mode,
            "wifi_connections": wifi_connections,
        }
    finally:
        await client.close()


@asynccontextmanager
async def _open_rsd(
    udid: str,
    host: Optional[str],
    port: Optional[int],
) -> AsyncIterator[object]:
    _require_tunnel_runtime()
    previous_provider = userspace_tunnel._create_no_root_tunnel_provider

    if host and port:
        async def provider(serial: Optional[str], autopair: bool) -> tuple[object, None]:
            service = await create_core_device_tunnel_service_using_remotepairing(
                udid,
                host,
                int(port),
                autopair=False,
            )
            return service, None
    else:
        async def provider(serial: Optional[str], autopair: bool) -> tuple[object, object]:
            lockdown = await _open_usb_lockdown(udid, autopair=autopair)
            return await CoreDeviceTunnelProxy.create(lockdown), lockdown

    userspace_tunnel._create_no_root_tunnel_provider = provider
    tunnel = userspace_tunnel.UserspaceRsdTunnel(serial=udid, autopair=False)
    try:
        async with tunnel as rsd:
            yield rsd
    finally:
        userspace_tunnel._create_no_root_tunnel_provider = previous_provider


def _battery_payload(raw: dict[str, object]) -> dict[str, object]:
    telemetry = raw.get("PowerTelemetryData")
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    raw_external = raw.get("ExternalConnected")
    external_state_available = isinstance(raw_external, (bool, int))
    external = bool(raw_external) if external_state_available else None
    charging = bool(raw.get("IsCharging"))
    raw_current = raw.get("InstantAmperage")
    if not isinstance(raw_current, (int, float)):
        raw_current = raw.get("Amperage")
    current = float(raw_current) if isinstance(raw_current, (int, float)) else None
    status = (
        "charging"
        if charging
        else "discharging"
        if current is not None and current < 0
        else "full"
        if raw.get("FullyCharged")
        else "idle"
    )
    temperature = raw.get("Temperature")
    return {
        "level_pct": raw.get("CurrentCapacity"),
        "voltage_mv": raw.get("Voltage"),
        "temperature_c": float(temperature) / 100.0 if isinstance(temperature, (int, float)) else None,
        "status": status,
        "powered": ["External"] if external is True else [],
        "external_connected": external,
        "external_power_state_available": external_state_available,
        "is_charging": charging,
        "fully_charged": bool(raw.get("FullyCharged")),
        "current_ma": current,
        "cycle_count": raw.get("CycleCount"),
        "design_capacity_mah": raw.get("DesignCapacity"),
        "nominal_charge_capacity_mah": raw.get("NominalChargeCapacity"),
        "full_charge_capacity_mah": raw.get("AppleRawMaxCapacity") or raw.get("FullChargeCapacity"),
        "maximum_capacity_pct": raw.get("MaxCapacity"),
        "update_time": raw.get("UpdateTime"),
        "power_telemetry": {
            key: telemetry[key]
            for key in (
                "BatteryPower",
                "SystemLoad",
                "SystemPowerIn",
                "SystemCurrentIn",
                "SystemVoltageIn",
                "SystemEnergyConsumed",
                "AccumulatedSystemEnergyConsumed",
            )
            if key in telemetry
        },
    }


def _battery_revision(raw: dict[str, object]) -> tuple[object, ...]:
    telemetry = raw.get("PowerTelemetryData")
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    return (
        raw.get("UpdateTime"),
        raw.get("InstantAmperage"),
        raw.get("Amperage"),
        raw.get("Voltage"),
        raw.get("Temperature"),
        telemetry.get("SystemLoad"),
        telemetry.get("BatteryPower"),
    )


def _selected_power_revision(raw: dict[str, object]) -> tuple[object, ...]:
    """Return a conservative revision for the power source selected for samples."""

    telemetry = raw.get("PowerTelemetryData")
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    system_load = telemetry.get("SystemLoad")
    if isinstance(system_load, (int, float)):
        return (
            "ios_power_telemetry_system_load",
            system_load,
            telemetry.get("SystemEnergyConsumed"),
            telemetry.get("AccumulatedSystemEnergyConsumed"),
        )

    raw_current = raw.get("InstantAmperage")
    if not isinstance(raw_current, (int, float)):
        raw_current = raw.get("Amperage")
    return (
        "ios_battery_current_voltage",
        raw_current,
        raw.get("Voltage"),
    )


def _sample_age_s(changed_host_monotonic_s: float) -> float:
    return max(0.0, time.monotonic() - changed_host_monotonic_s)


def _power_valid_for_consumption(
    direction: str,
    external_power: Optional[bool],
    power_source: str,
    power_sample_age_s: Optional[float],
) -> bool:
    if direction != "discharging" or external_power is not False:
        return False
    if power_source != "ios_power_telemetry_system_load":
        return True
    return bool(
        isinstance(power_sample_age_s, (int, float))
        and not isinstance(power_sample_age_s, bool)
        and math.isfinite(float(power_sample_age_s))
        and 0.0 <= float(power_sample_age_s) <= SYSTEM_LOAD_SAMPLE_STALE_AFTER_S
    )


def _display_parameter(
    parameters: dict[str, object],
    name: str,
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    value = parameters.get(name)
    value = value if isinstance(value, dict) else {}

    def number(key: str) -> Optional[float]:
        raw = value.get(key)
        if not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
            return None
        return float(raw)

    return number("value"), number("min"), number("max"), number("uncalMilliNits")


def _display_brightness_payload(raw: dict[str, object]) -> dict[str, object]:
    parameters = raw.get("IODisplayParameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    user_value, user_minimum, user_maximum, _ = _display_parameter(parameters, "brightness")
    raw_value, raw_minimum, raw_maximum, _ = _display_parameter(parameters, "rawBrightness")
    millinits, millinits_minimum, millinits_maximum, uncal_millinits = _display_parameter(
        parameters,
        "BrightnessMilliNits",
    )

    def fraction(
        value: Optional[float],
        minimum: Optional[float],
        maximum: Optional[float],
    ) -> Optional[float]:
        if value is None or minimum is None or maximum is None or maximum <= minimum:
            return None
        return max(0.0, min(1.0, (value - minimum) / (maximum - minimum)))

    user_fraction = fraction(user_value, user_minimum, user_maximum)
    raw_fraction = fraction(raw_value, raw_minimum, raw_maximum)
    luminance_nits = millinits / 1000.0 if millinits is not None else None
    uncalibrated_nits = uncal_millinits / 1000.0 if uncal_millinits is not None else None
    minimum_nits = millinits_minimum / 1000.0 if millinits_minimum is not None else None
    maximum_nits = millinits_maximum / 1000.0 if millinits_maximum is not None else None
    available = any(value is not None for value in (user_fraction, raw_value, luminance_nits))
    user_percent = user_fraction * 100.0 if user_fraction is not None else None
    screen_state = None
    if luminance_nits is not None or raw_value is not None:
        screen_state = (
            "OFF"
            if (luminance_nits or 0.0) <= 0.5 and (raw_value or 0.0) <= 0.0
            else "ON"
        )
    return {
        "available": available,
        "supported": available,
        "platform": "ios",
        "source": "AppleARMBacklight IODisplayParameters",
        "writable": False,
        "setter_mode": "read_only",
        "minimum": 0,
        "maximum": 100,
        "step": 1,
        "normalized_minimum": 0.0,
        "normalized_maximum": 1.0,
        "normalized_step": 0.01,
        "current": int(round(user_percent)) if user_percent is not None else None,
        "current_precise": user_percent,
        "setting_raw": user_percent,
        "setting_float": user_fraction,
        "current_screen_brightness": user_fraction,
        "user_brightness_pct": user_percent,
        "user_brightness_raw": user_value,
        "user_brightness_minimum_raw": user_minimum,
        "user_brightness_maximum_raw": user_maximum,
        "raw_backlight_raw": raw_value,
        "raw_backlight_minimum": raw_minimum,
        "raw_backlight_maximum": raw_maximum,
        "raw_backlight_fraction": raw_fraction,
        "luminance_nits": luminance_nits,
        "uncalibrated_luminance_nits": uncalibrated_nits,
        "minimum_luminance_nits": minimum_nits,
        "maximum_luminance_nits": maximum_nits,
        "automatic": None,
        "screen_state": screen_state,
        "limitations": (
            "iOS exposes AppleARMBacklight user brightness, raw backlight and calibrated milli-nits "
            "through diagnostics IORegistry, but it does not expose a trusted generic external setter "
            "or a public Auto-Brightness state through this sidecar."
        ),
    }


async def read_brightness_device(
    udid: str,
    host: Optional[str],
) -> dict[str, object]:
    _require_runtime()
    client = await (_open_tcp_lockdown(udid, host) if host else _open_usb_lockdown(udid, autopair=False))
    try:
        async with DiagnosticsService(client) as diagnostics:
            raw = dict(await diagnostics.ioregistry(ioclass="AppleARMBacklight") or {})
        result = _display_brightness_payload(raw)
        result["udid"] = udid
        if not result.get("available"):
            raise RuntimeError(
                "the iPhone did not expose AppleARMBacklight brightness parameters"
            )
        return result
    finally:
        await client.close()


async def probe_device(
    udid: str,
    host: Optional[str],
    port: Optional[int],
) -> dict[str, object]:
    _require_collection_runtime()
    device = await _device_values(udid, host)
    async with _open_rsd(udid, host, port) as rsd:
        async with DiagnosticsService(rsd) as diagnostics:
            raw_battery = await diagnostics.get_battery()
            try:
                raw_brightness = dict(
                    await diagnostics.ioregistry(ioclass="AppleARMBacklight") or {}
                )
            except Exception:
                raw_brightness = {}
        hardware: dict[str, object] = {}
        process_attributes: list[str] = []
        system_attributes: list[str] = []
        process_count: Optional[int] = None
        graphics: dict[str, object] = {}
        async with DvtProvider(rsd) as dvt:
            async with DeviceInfo(dvt) as info:
                hardware = dict(await info.hardware_information() or {})
                process_attributes = sorted(await info.sysmon_process_attributes())
                system_attributes = sorted(await info.sysmon_system_attributes())
                with suppress(Exception):
                    process_count = len(await info.proclist())
            try:
                async with Graphics(dvt) as monitor:
                    graphics = dict(await asyncio.wait_for(anext(monitor.__aiter__()), timeout=5))
            except Exception:
                graphics = {}

    cpu_count = hardware.get("numberOfCpus") or hardware.get("numberOfPhysicalCpus")
    device["cpu_count"] = cpu_count
    battery = _battery_payload(dict(raw_battery or {}))
    brightness = _display_brightness_payload(raw_brightness)
    power_telemetry = battery.get("power_telemetry")
    power_telemetry = power_telemetry if isinstance(power_telemetry, dict) else {}
    return {
        "platform": "ios",
        "device": device,
        "battery": battery,
        "brightness": brightness,
        "current_command": "IORegistry IOPMPowerSource / PowerTelemetryData",
        "current_command_ok": isinstance(battery.get("current_ma"), (int, float)),
        "cpu_policies": [],
        "gpu_source": {
            "name": "Apple GPU",
            "load_path": "DVT graphics Device Utilization %",
            "load_format": "percentage",
            "source_type": "ios_dvt_graphics",
        },
        "gpu_probe": {
            "provider": "ios_dvt_graphics",
            "device_utilization_pct": graphics.get("Device Utilization %"),
            "renderer_utilization_pct": graphics.get("Renderer Utilization %"),
            "tiler_utilization_pct": graphics.get("Tiler Utilization %"),
            "core_animation_fps": graphics.get("CoreAnimationFramesPerSecond"),
        },
        "foreground_package": None,
        "power_telemetry_available": any(
            isinstance(power_telemetry.get(key), (int, float))
            for key in ("SystemLoad", "BatteryPower", "SystemPowerIn")
        ),
        "system_monitor": {
            "process_top_available": bool(process_attributes),
            "process_count": process_count,
            "process_attributes": process_attributes,
            "system_attributes": system_attributes,
            "thermalservice_available": battery.get("temperature_c") is not None,
            "thermal_sensor_count": 1 if battery.get("temperature_c") is not None else 0,
            "thermal_threshold_count": 0,
            "cpusets": {},
            "cpu_policies": [],
            "adpf_available": False,
            "adpf_active_session_count": 0,
            "scheduler_warnings": [],
        },
        "capabilities": {
            "remote_xpc": True,
            "wireless": bool(host and port),
            "process_cpu": bool(process_attributes),
            "gpu_utilization": bool(graphics),
            "application_state_notifications": None,
            "application_state_notifications_status": "not_probed",
            "battery_power_update_hint_s": 20,
            "brightness_read": bool(brightness.get("available")),
            "brightness_control": False,
            "physical_luminance_nits": isinstance(
                brightness.get("luminance_nits"),
                (int, float),
            ),
        },
        "connection": {"type": "wireless" if host and port else "usb", "host": host, "port": port},
    }


def _number(value: object, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _mach_seconds(value: object, numer: float, denom: float) -> float:
    return _number(value) * numer / max(1.0, denom) / 1_000_000_000.0


def _process_snapshot(
    processes: list[dict[str, object]],
    uptime_s: float,
    applications: dict[str, dict[str, object]],
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    watched: list[dict[str, object]] = []
    for process in processes:
        name = str(process.get("name") or process.get("comm") or "unknown")
        cpu = _number(process.get("cpuUsage"))
        uid = process.get("uid")
        sandboxed = bool(process.get("__sandbox"))
        application = applications.get(name)
        is_application = application is not None
        category = (
            "kernel"
            if name == "kernel_task"
            else "application"
            if is_application
            else "native_system"
        )
        row = {
            "pid": int(_number(process.get("pid"))),
            "ppid": int(_number(process.get("ppid"))),
            "user": "" if uid is None else str(uid),
            "name": name,
            "command": name,
            "category": category,
            "policy": process.get("policy"),
            "state": process.get("procStatus"),
            "app_sleep": process.get("appSleep"),
            "sandboxed": sandboxed,
            "bundle_id": application.get("bundle_id") if application else None,
            "display_name": application.get("display_name") if application else None,
            "cpu_pct": cpu,
            "resident_bytes": int(_number(process.get("physFootprint"))),
            "thread_count": int(_number(process.get("threadCount"))),
            "disk_bytes_read": int(_number(process.get("diskBytesRead"))),
            "disk_bytes_written": int(_number(process.get("diskBytesWritten"))),
            "power_score": process.get("powerScore"),
            "average_power_score": process.get("avgPowerScore"),
            "total_energy_score": process.get("totalEnergyScore"),
            "platform": "ios",
        }
        rows.append(row)
        if name in COLLECTOR_PROCESSES:
            watched.append(
                {
                    **row,
                    "watch_name": "ios_profiler",
                    "watch_label": "iOS Instruments collector overhead",
                    "watch_kind": "profiler_overhead",
                    "watch_impact": "iOS Instruments 采集器本身会占用设备 CPU，比较运行时应保持同一采集配置。",
                    "activity_family": "profiler_overhead",
                    "subsystem": "ios_instruments",
                    "activity_active": cpu > 0.0,
                }
            )
    rows.sort(key=lambda item: _number(item.get("cpu_pct")), reverse=True)
    return {
        "uptime_s": uptime_s,
        "host_epoch_s": time.time(),
        "processes": rows[:80],
        "threads": [],
        "watched_processes": watched,
        "process_count": len(rows),
        "thread_count": sum(int(_number(item.get("thread_count"))) for item in rows),
    }


async def record_device(args: argparse.Namespace) -> None:
    _require_collection_runtime()
    duration_s = 0.0 if bool(getattr(args, "unlimited", False)) else float(args.duration)
    udid = str(args.udid)
    host = str(args.host) if args.host else None
    port = int(args.port) if args.port else None
    device = await _device_values(udid, host)
    async with _open_rsd(udid, host, port) as rsd:
        async with DiagnosticsService(rsd) as diagnostics:
            initial_raw = dict(await diagnostics.get_battery() or {})
            if args.no_display_brightness:
                initial_brightness_raw: dict[str, object] = {}
            else:
                try:
                    initial_brightness_raw = dict(
                        await diagnostics.ioregistry(ioclass="AppleARMBacklight") or {}
                    )
                except Exception:
                    initial_brightness_raw = {}
        latest_battery = initial_raw
        latest_battery_revision = _battery_revision(initial_raw)
        latest_power_revision = _selected_power_revision(initial_raw)
        latest_power_monotonic = time.monotonic()
        latest_brightness = _display_brightness_payload(initial_brightness_raw)
        latest_thermal_notification: dict[str, object] = {}
        brightness_sample_count = 0
        latest_graphics: dict[str, object] = {}
        latest_graphics_monotonic: Optional[float] = None
        latest_uptime = 0.0
        current_app: Optional[dict[str, object]] = None
        notifications_observed = False
        applications: dict[str, dict[str, object]] = {}
        stop = asyncio.Event()
        collector_values: list[float] = []

        async with DvtProvider(rsd) as dvt:
            async with DeviceInfo(dvt) as info:
                hardware = dict(await info.hardware_information() or {})
                clock_before_epoch = time.time()
                clock_before_monotonic = time.monotonic()
                mach_info = list(await info.mach_time_info() or [])
                clock_after_monotonic = time.monotonic()
                clock_after_epoch = time.time()
                process_attributes = list(await info.sysmon_process_attributes())
                system_attributes = list(await info.sysmon_system_attributes())

            if ApplicationListing is not None:
                try:
                    async with ApplicationListing(dvt) as listing:
                        installed_apps = list(await listing.applist())
                    for app in installed_apps:
                        if not isinstance(app, dict):
                            continue
                        bundle_path = str(app.get("BundlePath") or "")
                        if not bundle_path.startswith("/private/var/containers/Bundle/Application/"):
                            continue
                        executable = str(app.get("ExecutableName") or "").strip()
                        display_name = str(app.get("DisplayName") or "").strip()
                        bundle_id = str(app.get("CFBundleIdentifier") or "").strip()
                        if not executable or not bundle_id:
                            continue
                        value = {
                            "bundle_id": bundle_id,
                            "display_name": display_name or executable,
                            "executable": executable,
                        }
                        applications[executable] = value
                        if display_name:
                            applications.setdefault(display_name, value)
                except Exception as exc:
                    _emit("warning", message=f"iOS application listing unavailable: {exc}")

            cpu_count = max(1.0, _number(hardware.get("numberOfCpus"), 1.0))
            numer = _number(mach_info[1], 1.0) if len(mach_info) > 1 else 1.0
            denom = _number(mach_info[2], 1.0) if len(mach_info) > 2 else 1.0
            clock_uptime = _mach_seconds(mach_info[0], numer, denom) if mach_info else 0.0
            clock_epoch = (clock_before_epoch + clock_after_epoch) / 2.0
            clock_monotonic = (clock_before_monotonic + clock_after_monotonic) / 2.0
            clock_round_trip_ms = max(
                0.0,
                (clock_after_monotonic - clock_before_monotonic) * 1000.0,
            )
            latest_uptime = clock_uptime
            _emit(
                "ready",
                device={**device, "cpu_count": int(cpu_count)},
                hardware=hardware,
                battery=_battery_payload(initial_raw),
                brightness=latest_brightness,
                process_attributes=sorted(process_attributes),
                system_attributes=sorted(system_attributes),
                clock={
                    "host_epoch_s": clock_epoch,
                    "host_monotonic_s": clock_monotonic,
                    "device_uptime_s": clock_uptime,
                    "round_trip_ms": clock_round_trip_ms,
                },
            )

            async def battery_loop() -> None:
                nonlocal latest_battery, latest_battery_revision
                nonlocal latest_power_revision, latest_power_monotonic
                nonlocal latest_brightness, brightness_sample_count
                warned_battery = False
                warned_brightness = False
                while not stop.is_set():
                    try:
                        async with DiagnosticsService(rsd) as service:
                            value = dict(await service.get_battery() or {})
                            if args.no_display_brightness:
                                raw_brightness: dict[str, object] = {}
                            else:
                                try:
                                    raw_brightness = dict(
                                        await service.ioregistry(ioclass="AppleARMBacklight") or {}
                                    )
                                except Exception as exc:
                                    raw_brightness = {}
                                    if not warned_brightness:
                                        _emit(
                                            "warning",
                                            message=f"iOS display brightness polling failed: {exc}",
                                        )
                                        warned_brightness = True
                        latest_battery = value
                        power_revision = _selected_power_revision(value)
                        if power_revision != latest_power_revision:
                            latest_power_revision = power_revision
                            latest_power_monotonic = time.monotonic()
                        revision = _battery_revision(value)
                        if revision != latest_battery_revision:
                            latest_battery_revision = revision
                        battery = _battery_payload(value)
                        temperature = battery.get("temperature_c")
                        if raw_brightness:
                            latest_brightness = _display_brightness_payload(raw_brightness)
                            brightness_sample_count += 1
                        display_brightness = dict(latest_brightness)
                        notification_time = latest_thermal_notification.get("host_epoch_s")
                        notification_age = (
                            max(0.0, time.time() - float(notification_time))
                            if isinstance(notification_time, (int, float))
                            else None
                        )
                        if notification_age is not None and notification_age <= 120.0:
                            display_brightness.update(
                                {
                                    "thermal_notification": latest_thermal_notification.get("name"),
                                    "thermal_notification_age_s": notification_age,
                                    "thermal_notification_active": bool(
                                        latest_thermal_notification.get("active")
                                    ),
                                }
                            )
                        temperatures = (
                            [
                                {
                                    "name": "Battery",
                                    "type": "BATTERY",
                                    "value_c": temperature,
                                    "status": None,
                                }
                            ]
                            if isinstance(temperature, (int, float))
                            else []
                        )
                        if temperatures or display_brightness.get("available"):
                            _emit(
                                "thermal",
                                snapshot={
                                    "uptime_s": latest_uptime,
                                    "host_epoch_s": time.time(),
                                    "status": None,
                                    "hal_ready": True,
                                    "temperatures": temperatures,
                                    "cooling_devices": [],
                                    "thresholds": [],
                                    "headroom_thresholds": [],
                                    "display_brightness": display_brightness,
                                },
                            )
                    except Exception as exc:
                        if not warned_battery:
                            _emit("warning", message=f"iOS battery polling failed: {exc}")
                            warned_battery = True
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        pass

            async def thermal_notifications_loop() -> None:
                nonlocal latest_thermal_notification
                names = (
                    "com.apple.system.earlythermalnotification",
                    "com.apple.system.thermalpressurelevel",
                    "com.apple.system.thermalpressurelevel.cold",
                )
                try:
                    async with NotificationProxyService(rsd) as notifications:
                        for name in names:
                            await notifications.notify_register_dispatch(name)
                        async for event in notifications.receive_notification():
                            if stop.is_set():
                                return
                            if not isinstance(event, dict):
                                continue
                            name = str(event.get("Name") or "").strip()
                            if name not in names:
                                continue
                            latest_thermal_notification = {
                                "name": name,
                                "host_epoch_s": time.time(),
                                "active": not name.endswith(".cold"),
                            }
                except Exception as exc:
                    _emit(
                        "warning",
                        message=f"iOS thermal-pressure notifications unavailable: {exc}",
                    )

            async def graphics_loop() -> None:
                nonlocal latest_graphics, latest_graphics_monotonic
                try:
                    async with Graphics(dvt) as graphics:
                        async for event in graphics:
                            if stop.is_set():
                                return
                            if isinstance(event, dict):
                                latest_graphics = dict(event)
                                latest_graphics_monotonic = time.monotonic()
                except Exception as exc:
                    _emit("warning", message=f"iOS GPU graphics stream unavailable: {exc}")

            async def notifications_loop() -> None:
                nonlocal current_app, notifications_observed
                try:
                    async with Notifications(dvt) as notifications:
                        async for event in notifications:
                            if stop.is_set():
                                return
                            if not isinstance(event, tuple) or len(event) != 2:
                                continue
                            selector, arguments = event
                            if selector != "applicationStateNotification:" or not arguments:
                                continue
                            value = arguments[0]
                            if not isinstance(value, dict):
                                continue
                            notifications_observed = True
                            state = str(value.get("state_description") or "")
                            app_name = str(value.get("appName") or "").strip() or None
                            executable = str(value.get("execName") or "").strip() or None
                            application = applications.get(executable or "") or applications.get(app_name or "")
                            bundle_id = (
                                str(application.get("bundle_id"))
                                if isinstance(application, dict) and application.get("bundle_id")
                                else app_name
                            )
                            event_uptime = _mach_seconds(value.get("mach_absolute_time"), numer, denom)
                            if state == "Running" and app_name:
                                current_app = {
                                    "name": app_name,
                                    "package": bundle_id,
                                    "executable": executable,
                                }
                                _emit(
                                    "context",
                                    context={
                                        "uptime_s": event_uptime or latest_uptime,
                                        "foreground_package": bundle_id,
                                        "foreground_activity": executable,
                                        "screen_state": None,
                                        "screen_state": latest_brightness.get("screen_state"),
                                        "brightness_raw": latest_brightness.get("setting_raw"),
                                        "refresh_rate_hz": None,
                                        "source": "ios_dvt_notifications",
                                    },
                                )
                            elif (
                                state == "Suspended"
                                and current_app is not None
                                and app_name == current_app.get("name")
                            ):
                                current_app = None
                                _emit(
                                    "context",
                                    context={
                                        "uptime_s": event_uptime or latest_uptime,
                                        "foreground_package": None,
                                        "foreground_activity": None,
                                        "screen_state": None,
                                        "screen_state": latest_brightness.get("screen_state"),
                                        "brightness_raw": latest_brightness.get("setting_raw"),
                                        "refresh_rate_hz": None,
                                        "source": "ios_dvt_notifications",
                                    },
                                )
                except Exception as exc:
                    _emit("warning", message=f"iOS application-state stream unavailable: {exc}")

            tap = Sysmontap(
                dvt,
                process_attributes,
                system_attributes,
                interval_ms=max(200, int(float(args.interval) * 1000.0)),
            )
            fields = [field.name for field in dataclasses.fields(tap.process_attributes_cls)]
            tasks = [
                asyncio.create_task(battery_loop(), name="ios-battery"),
                asyncio.create_task(graphics_loop(), name="ios-graphics"),
                asyncio.create_task(notifications_loop(), name="ios-notifications"),
            ]
            if not args.no_display_brightness:
                tasks.append(
                    asyncio.create_task(
                        thermal_notifications_loop(),
                        name="ios-thermal-notifications",
                    )
                )
            interval_s = max(0.001, float(args.interval))
            target_coverage_s = _ios_collection_target_coverage_s(
                duration_s,
                interval_s,
            )
            deadline = (
                math.inf
                if duration_s <= 0
                else time.monotonic()
                + duration_s
                + max(
                    5.0,
                    interval_s * 2.0,
                )
            )
            first_sample_uptime: Optional[float] = None
            sample_index = 0
            next_process_snapshot = 0.0
            next_clock_sync = clock_uptime + float(args.clock_interval)
            try:
                async with tap:
                    async for row in tap:
                        if "Processes" not in row:
                            if time.monotonic() >= deadline:
                                break
                            continue
                        processes = [
                            dict(zip(fields, values))
                            for values in dict(row.get("Processes") or {}).values()
                        ]
                        cpu_visible = [
                            item for item in processes if isinstance(item.get("cpuUsage"), (int, float))
                        ]
                        if not cpu_visible:
                            if time.monotonic() >= deadline:
                                break
                            continue
                        uptime = _mach_seconds(row.get("EndMachAbsTime"), numer, denom)
                        if uptime <= 0:
                            uptime = clock_uptime + (time.monotonic() - clock_monotonic)
                        if first_sample_uptime is None:
                            first_sample_uptime = uptime
                            if duration_s > 0:
                                deadline = time.monotonic() + target_coverage_s + max(
                                    5.0,
                                    interval_s * 2.0,
                                )
                        latest_uptime = uptime
                        total_process_cpu = sum(_number(item.get("cpuUsage")) for item in cpu_visible)
                        collector_cpu = sum(
                            _number(item.get("cpuUsage"))
                            for item in cpu_visible
                            if str(item.get("name") or item.get("comm") or "") in COLLECTOR_PROCESSES
                        )
                        normalized_cpu = max(0.0, min(100.0, total_process_cpu / cpu_count))
                        normalized_collector_cpu = max(0.0, collector_cpu / cpu_count)
                        collector_values.append(normalized_collector_cpu)

                        battery = latest_battery
                        telemetry = battery.get("PowerTelemetryData")
                        telemetry = telemetry if isinstance(telemetry, dict) else {}
                        voltage = _number(battery.get("Voltage"))
                        signed_current = battery.get("InstantAmperage")
                        if not isinstance(signed_current, (int, float)):
                            signed_current = battery.get("Amperage")
                        signed_current = _number(signed_current)
                        system_load = telemetry.get("SystemLoad")
                        if isinstance(system_load, (int, float)):
                            power_mw = max(0.0, float(system_load))
                            power_source = "ios_power_telemetry_system_load"
                        else:
                            power_mw = abs(signed_current) * voltage / 1000.0
                            power_source = "ios_battery_current_voltage"
                        raw_external = battery.get("ExternalConnected")
                        external = (
                            bool(raw_external)
                            if isinstance(raw_external, (bool, int))
                            else None
                        )
                        charging = bool(battery.get("IsCharging"))
                        direction = (
                            "charging"
                            if charging or signed_current > 0
                            else "discharging"
                            if signed_current < 0
                            else "idle"
                        )
                        power_age = _sample_age_s(latest_power_monotonic)
                        temperature = battery.get("Temperature")
                        gpu_age = (
                            _sample_age_s(latest_graphics_monotonic)
                            if latest_graphics_monotonic is not None
                            else None
                        )
                        gpu_stale = (
                            gpu_age is None or gpu_age > GPU_SAMPLE_STALE_AFTER_S
                        )
                        raw_gpu_load = latest_graphics.get("Device Utilization %")
                        gpu_load = (
                            float(raw_gpu_load)
                            if not gpu_stale and isinstance(raw_gpu_load, (int, float))
                            else None
                        )
                        _emit(
                            "sample",
                            sample={
                                "index": sample_index,
                                "elapsed_s": max(0.0, uptime - clock_uptime),
                                "uptime_s": uptime,
                                "current_ma": abs(signed_current),
                                "signed_current_ma": signed_current,
                                "voltage_mv": voltage,
                                "power_mw": power_mw,
                                "direction": direction,
                                "cpu_pct": normalized_cpu,
                                "core_cpu_pct": {},
                                "cluster_cpu_pct": {},
                                "frequencies_mhz": {},
                                "gpu_frequency_mhz": None,
                                "gpu_load_pct": gpu_load,
                                "gpu_sample_age_s": gpu_age,
                                "gpu_sample_stale": gpu_stale,
                                "gpu_sample_available": gpu_load is not None,
                                "gpu_sample_stale_after_s": GPU_SAMPLE_STALE_AFTER_S,
                                "battery_temperature_c": (
                                    float(temperature) / 100.0
                                    if isinstance(temperature, (int, float))
                                    else None
                                ),
                                "power_source": power_source,
                                "power_sample_age_s": power_age,
                                "collector_cpu_pct": normalized_collector_cpu,
                                "external_power": external,
                                "power_valid_for_consumption": _power_valid_for_consumption(
                                    direction,
                                    external,
                                    power_source,
                                    power_age,
                                ),
                            },
                        )
                        sample_index += 1

                        if uptime >= next_clock_sync:
                            _emit(
                                "clock",
                                clock={
                                    "host_epoch_s": time.time(),
                                    "host_monotonic_s": time.monotonic(),
                                    "device_uptime_s": uptime,
                                    "round_trip_ms": 0.0,
                                },
                            )
                            next_clock_sync = uptime + float(args.clock_interval)

                        if not args.no_system_monitor and uptime >= next_process_snapshot:
                            _emit(
                                "system",
                                snapshot=_process_snapshot(cpu_visible, uptime, applications),
                            )
                            next_process_snapshot = uptime + float(args.process_interval)
                        if (
                            first_sample_uptime is not None
                            and _ios_collection_window_complete(
                                first_sample_uptime,
                                uptime,
                                duration_s,
                                interval_s,
                            )
                        ) or time.monotonic() >= deadline:
                            break
            finally:
                stop.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        try:
            async with DiagnosticsService(rsd) as diagnostics:
                final_raw = dict(await diagnostics.get_battery() or {})
        except Exception:
            final_raw = latest_battery
        _emit(
            "end",
            battery=_battery_payload(final_raw),
            stats={
                "sample_count": sample_index,
                "brightness_sample_count": brightness_sample_count,
                "brightness_source": (
                    "AppleARMBacklight IODisplayParameters"
                    if brightness_sample_count
                    else None
                ),
                "average_collector_cpu_pct": (
                    sum(collector_values) / len(collector_values) if collector_values else None
                ),
                "battery_update_hint_s": 20,
                "gpu_stream_observed": latest_graphics_monotonic is not None,
                "gpu_last_sample_age_s": (
                    _sample_age_s(latest_graphics_monotonic)
                    if latest_graphics_monotonic is not None
                    else None
                ),
                "gpu_sample_stale_after_s": GPU_SAMPLE_STALE_AFTER_S,
                "application_state_notifications_observed": notifications_observed,
            },
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="iOS sidecar for Mobile Profiler")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")

    pair = subparsers.add_parser("pair")
    pair.add_argument("--udid")
    pair.add_argument("--timeout", type=float, default=8.0)

    probe = subparsers.add_parser("probe")
    probe.add_argument("--udid", required=True)
    probe.add_argument("--host")
    probe.add_argument("--port", type=int)

    brightness = subparsers.add_parser("brightness")
    brightness.add_argument("--udid", required=True)
    brightness.add_argument("--host")
    brightness.add_argument("--port", type=int)

    record = subparsers.add_parser("record")
    record.add_argument("--udid", required=True)
    record.add_argument("--host")
    record.add_argument("--port", type=int)
    duration = record.add_mutually_exclusive_group(required=True)
    duration.add_argument("--duration", type=float)
    duration.add_argument("--unlimited", action="store_true")
    record.add_argument("--interval", type=float, default=1.0)
    record.add_argument("--process-interval", type=float, default=10.0)
    record.add_argument("--clock-interval", type=float, default=30.0)
    record.add_argument("--no-system-monitor", action="store_true")
    record.add_argument("--no-display-brightness", action="store_true")
    return parser


async def async_main(args: argparse.Namespace) -> int:
    if args.command == "list":
        _print_json(await list_devices())
    elif args.command == "pair":
        _print_json(await pair_device(args.udid, args.timeout))
    elif args.command == "probe":
        _print_json(await probe_device(args.udid, args.host, args.port))
    elif args.command == "brightness":
        _print_json(await read_brightness_device(args.udid, args.host))
    elif args.command == "record":
        await record_device(args)
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 130
    except BaseException as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
