from __future__ import annotations

import json
import ipaddress
import math
import os
import shutil
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
IOS_STATE_DIR = Path(
    os.environ.get(
        "MOBILE_PROFILER_STATE_DIR",
        Path.home() / ".mobile-profiler",
    )
).expanduser()
IOS_ENDPOINTS_PATH = IOS_STATE_DIR / "ios-devices.json"


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
    except subprocess.TimeoutExpired as exc:
        operation = str(arguments[0]) if arguments else "operation"
        raise RuntimeError(
            f"iOS {operation} timed out after {timeout_s:.0f} seconds"
        ) from exc
    except (OSError, TypeError) as exc:
        raise RuntimeError(f"iOS sidecar could not start: {exc}") from exc
    if result.returncode != 0:
        message = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"iOS sidecar exited with code {result.returncode}"
        )
        if message.upper().startswith("ERROR:"):
            message = message.split(":", 1)[1].strip()
        raise RuntimeError(message)
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
    return _read_endpoints(IOS_ENDPOINTS_PATH)


def _write_endpoints(value: Dict[str, Dict[str, object]]) -> None:
    IOS_ENDPOINTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = IOS_ENDPOINTS_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(IOS_ENDPOINTS_PATH)


def _normalize_ios_wireless_transport(value: object) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    if text in {"bluetooth", "bluetooth-pan", "pan"}:
        return "bluetooth-pan"
    if text in {"wifi", "wi-fi", "wlan"}:
        return "wifi"
    if text in {"usb-ncm", "link-local"}:
        return "usb-ncm"
    return "unknown"


def _ios_wireless_transport_label(value: object) -> str:
    return {
        "bluetooth-pan": "Bluetooth PAN",
        "wifi": "Wi-Fi",
        "usb-ncm": "USB-NCM link-local",
        "unknown": "unconfirmed wireless transport",
    }[_normalize_ios_wireless_transport(value)]


def _save_endpoint(
    udid: str,
    host: object,
    port: object,
    device: Optional[Dict[str, object]] = None,
    *,
    wireless_transport: Optional[object] = None,
    transport_source: Optional[object] = None,
    local_address: Optional[object] = None,
    adapter_name: Optional[object] = None,
    adapter_index: Optional[object] = None,
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
    if wireless_transport is not None:
        current["wireless_transport"] = _normalize_ios_wireless_transport(
            wireless_transport
        )
    if transport_source:
        current["transport_source"] = str(transport_source)
    if local_address:
        current["local_address"] = str(local_address)
    if adapter_name:
        current["adapter_name"] = str(adapter_name)
    if isinstance(adapter_index, (int, float)):
        current["adapter_index"] = int(adapter_index)
    endpoints[udid] = current
    _write_endpoints(endpoints)


def _endpoint_reachable(
    host: object,
    port: object,
    timeout_s: float = 0.5,
    attempts: int = 3,
) -> bool:
    if not host or not isinstance(port, (int, float)):
        return False
    for _ in range(max(1, int(attempts))):
        try:
            with socket.create_connection((str(host), int(port)), timeout=timeout_s):
                return True
        except OSError:
            continue
    return False


def _endpoint_addresses(host: object) -> List[ipaddress._BaseAddress]:
    text = str(host or "").strip().strip("[]")
    if not text:
        return []
    literal = text.split("%", 1)[0]
    try:
        return [ipaddress.ip_address(literal)]
    except ValueError:
        pass
    addresses: List[ipaddress._BaseAddress] = []
    try:
        results = socket.getaddrinfo(text, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    for result in results:
        try:
            address = ipaddress.ip_address(str(result[4][0]).split("%", 1)[0])
        except (IndexError, ValueError):
            continue
        if address not in addresses:
            addresses.append(address)
    return addresses


def _endpoint_scope(host: object) -> str:
    addresses = _endpoint_addresses(host)
    if not addresses:
        return "unknown"
    usable = [
        address
        for address in addresses
        if not address.is_loopback
        and not address.is_unspecified
        and not address.is_multicast
    ]
    if not usable:
        return "local-only"
    if all(address.is_link_local for address in usable):
        return "link-local"
    if any(address.is_private and not address.is_link_local for address in usable):
        return "private-lan"
    return "routed"


def _endpoint_is_unplug_candidate(host: object) -> bool:
    return _endpoint_scope(host) in {"private-lan", "routed"}


_WINDOWS_ENDPOINT_ROUTE_SCRIPT = r"""
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$target = [string]$env:MOBILE_PROFILER_IOS_ENDPOINT_HOST
$selection = @(Find-NetRoute -RemoteIPAddress $target -ErrorAction Stop)
$route = $selection |
    Where-Object { $null -ne $_.InterfaceIndex } |
    Select-Object -First 1
if ($null -eq $route) {
    throw "Windows did not return a route for the iOS endpoint"
}
$adapter = Get-NetAdapter `
    -InterfaceIndex $route.InterfaceIndex `
    -IncludeHidden `
    -ErrorAction Stop
$source = $selection |
    Where-Object { $_.IPAddress } |
    Select-Object -First 1
[pscustomobject]@{
    interface_alias = [string]$adapter.InterfaceAlias
    interface_description = [string]$adapter.InterfaceDescription
    interface_index = [int]$adapter.InterfaceIndex
    status = [string]$adapter.Status
    physical_media_type = [string]$adapter.PhysicalMediaType
    media_type = [string]$adapter.MediaType
    ndis_physical_medium = [int]$adapter.NdisPhysicalMedium
    pnp_device_id = [string]$adapter.PNPDeviceID
    local_address = [string]$source.IPAddress
} | ConvertTo-Json -Compress
"""


def _classify_windows_route_adapter(adapter: Dict[str, object]) -> str:
    try:
        ndis_medium = int(adapter.get("ndis_physical_medium") or -1)
    except (TypeError, ValueError):
        ndis_medium = -1
    fingerprint = " ".join(
        str(adapter.get(key) or "")
        for key in (
            "interface_alias",
            "interface_description",
            "physical_media_type",
            "media_type",
            "pnp_device_id",
        )
    ).lower()
    if (
        ndis_medium == 10
        or "bluetooth" in fingerprint
        or "blue tooth" in fingerprint
        or "ms_bthpan" in fingerprint
        or "蓝牙" in fingerprint
        or "藍牙" in fingerprint
    ):
        return "bluetooth-pan"
    if (
        ndis_medium in {1, 9}
        or "native 802.11" in fingerprint
        or "wi-fi" in fingerprint
        or "wifi" in fingerprint
        or "wireless lan" in fingerprint
        or "wlan" in fingerprint
    ):
        return "wifi"
    return "unknown"


def _windows_endpoint_route(host: object) -> Dict[str, object]:
    if sys.platform != "win32" or not host:
        return {}
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return {}
    try:
        result = _run_subprocess(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _WINDOWS_ENDPOINT_ROUTE_SCRIPT,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=6.0,
            check=False,
            env={
                **os.environ,
                "MOBILE_PROFILER_IOS_ENDPOINT_HOST": str(host),
            },
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, TypeError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    for line in reversed(result.stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _ios_wireless_transport_details(
    host: object,
    cached: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    scope = _endpoint_scope(host)
    if scope == "link-local":
        return {
            "wireless_transport": "usb-ncm",
            "transport_source": "endpoint-scope",
        }
    route = _windows_endpoint_route(host)
    if route:
        return {
            "wireless_transport": _classify_windows_route_adapter(route),
            "transport_source": "windows-route",
            "local_address": str(route.get("local_address") or ""),
            "adapter_name": str(
                route.get("interface_description")
                or route.get("interface_alias")
                or ""
            ),
            "adapter_index": route.get("interface_index"),
        }
    cached = cached if isinstance(cached, dict) else {}
    cached_host = str(cached.get("host") or "")
    cached_transport = _normalize_ios_wireless_transport(
        cached.get("wireless_transport")
    )
    if str(host or "") == cached_host and cached_transport != "unknown":
        return {
            "wireless_transport": cached_transport,
            "transport_source": "cache",
            "local_address": str(cached.get("local_address") or ""),
            "adapter_name": str(cached.get("adapter_name") or ""),
            "adapter_index": cached.get("adapter_index"),
        }
    return {
        "wireless_transport": "unknown",
        "transport_source": "unavailable",
    }


_WINDOWS_BLUETOOTH_PAN_SCRIPT = r"""
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$deviceName = [string]$env:MOBILE_PROFILER_IOS_BLUETOOTH_NAME
$timeoutSeconds = [Math]::Max(
    8,
    [int]($env:MOBILE_PROFILER_IOS_BLUETOOTH_TIMEOUT_SECONDS)
)
$openedWindow = $false
$panWindow = $null

Add-Type -AssemblyName UIAutomationClient
Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;

public static class MobileProfilerPanNative
{
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetClassName(IntPtr hWnd, StringBuilder value, int length);

    [DllImport("user32.dll")]
    public static extern IntPtr SendMessage(IntPtr hWnd, uint message, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool PostMessage(IntPtr hWnd, uint message, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern int GetMenuItemCount(IntPtr menu);
}
'@

function Get-PanAdapter {
    return @(
        Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue |
            Where-Object {
                $_.InterfaceDescription -match "Bluetooth.*Personal Area Network"
            } |
            Sort-Object ifIndex
    ) | Select-Object -First 1
}

function Get-PanConnection([bool]$alreadyConnected) {
    $adapter = Get-PanAdapter
    if ($null -eq $adapter) {
        throw "Windows Bluetooth Personal Area Network adapter was not found"
    }
    $configuration = Get-NetIPConfiguration `
        -InterfaceIndex $adapter.ifIndex `
        -ErrorAction SilentlyContinue
    $address = @(
        $configuration.IPv4Address |
            Where-Object {
                $_.IPAddress -and $_.IPAddress -notlike "169.254.*"
            }
    ) | Select-Object -First 1
    $gateway = @(
        $configuration.IPv4DefaultGateway |
            Where-Object { $_.NextHop }
    ) | Select-Object -First 1
    if (
        $adapter.Status -eq "Up" -and
        $null -ne $address -and
        $null -ne $gateway
    ) {
        return [pscustomobject]@{
            adapter_name = [string]$adapter.InterfaceDescription
            address = [string]$address.IPAddress
            gateway = [string]$gateway.NextHop
            link_speed = [string]$adapter.LinkSpeed
            already_connected = $alreadyConnected
        }
    }
    return $null
}

function Get-TopLevelHandles {
    $script:mobileProfilerPanHandles = `
        New-Object System.Collections.Generic.List[System.IntPtr]
    [MobileProfilerPanNative]::EnumWindows(
        {
            param($handle, $parameter)
            $script:mobileProfilerPanHandles.Add($handle)
            return $true
        },
        [System.IntPtr]::Zero
    ) | Out-Null
    return @($script:mobileProfilerPanHandles)
}

function Get-WindowClass([System.IntPtr]$handle) {
    $value = New-Object System.Text.StringBuilder 128
    [void][MobileProfilerPanNative]::GetClassName($handle, $value, $value.Capacity)
    return $value.ToString()
}

$connectButtonCondition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
    "{2AEAF233-E041-11DD-B847-AE2D00021148}"
)

function Find-PanWindow {
    foreach ($handle in Get-TopLevelHandles) {
        try {
            $root = [System.Windows.Automation.AutomationElement]::FromHandle($handle)
            $button = $root.FindFirst(
                [System.Windows.Automation.TreeScope]::Descendants,
                $connectButtonCondition
            )
            if ($null -ne $button) {
                return [pscustomobject]@{
                    handle = $handle
                    root = $root
                    button = $button
                }
            }
        }
        catch {
        }
    }
    return $null
}

function Get-PopupHandles {
    $values = @()
    foreach ($handle in Get-TopLevelHandles) {
        if ((Get-WindowClass $handle) -eq "#32768") {
            $values += $handle
        }
    }
    return $values
}

function Find-ConnectPopup([hashtable]$excluded) {
    foreach ($handle in Get-PopupHandles) {
        $key = [string][long]$handle
        if ($excluded.ContainsKey($key)) {
            continue
        }
        $menu = [MobileProfilerPanNative]::SendMessage(
            $handle,
            0x01E1,
            [System.IntPtr]::Zero,
            [System.IntPtr]::Zero
        )
        if (
            $menu -ne [System.IntPtr]::Zero -and
            [MobileProfilerPanNative]::GetMenuItemCount($menu) -ge 1
        ) {
            return $handle
        }
    }
    return [System.IntPtr]::Zero
}

try {
    $connection = Get-PanConnection $true
    if ($null -ne $connection) {
        Write-Output ($connection | ConvertTo-Json -Compress)
        return
    }

    $adapter = Get-PanAdapter
    if ($null -eq $adapter) {
        throw "Windows Bluetooth Personal Area Network adapter was not found"
    }
    if ($adapter.Status -eq "Disabled") {
        throw "Windows Bluetooth Personal Area Network adapter is disabled"
    }

    $panWindow = Find-PanWindow
    if ($null -eq $panWindow) {
        $shell = New-Object -ComObject Shell.Application
        $networkFolder = $shell.Namespace(
            "shell:::{7007ACC7-3202-11D1-AAD2-00805FC1270E}"
        )
        $adapterItem = @($networkFolder.Items()) |
            Where-Object { $_.Name -eq $adapter.Name } |
            Select-Object -First 1
        if ($null -eq $adapterItem) {
            throw "Windows Bluetooth network connection entry was not found"
        }
        $viewVerb = @($adapterItem.Verbs()) |
            Where-Object {
                (($_.Name -replace "&", "").Trim()) -match `
                    "Bluetooth.*Network|蓝牙.*网络"
            } |
            Select-Object -First 1
        if ($null -eq $viewVerb) {
            $viewVerb = @($adapterItem.Verbs()) | Select-Object -First 1
        }
        if ($null -eq $viewVerb) {
            throw "Windows Bluetooth network device view is unavailable"
        }
        $viewVerb.DoIt()
        $openedWindow = $true
        $deadline = (Get-Date).AddSeconds(10)
        do {
            Start-Sleep -Milliseconds 200
            $panWindow = Find-PanWindow
        } while ($null -eq $panWindow -and (Get-Date) -lt $deadline)
    }
    if ($null -eq $panWindow) {
        throw "Windows Bluetooth network device window did not open"
    }

    $listItemCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::ListItem
    )
    $nameCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        $deviceName
    )
    $phoneCondition = New-Object System.Windows.Automation.AndCondition(
        $listItemCondition,
        $nameCondition
    )
    $phone = $null
    $deadline = (Get-Date).AddSeconds(8)
    do {
        Start-Sleep -Milliseconds 200
        $phone = $panWindow.root.FindFirst(
            [System.Windows.Automation.TreeScope]::Descendants,
            $phoneCondition
        )
    } while ($null -eq $phone -and (Get-Date) -lt $deadline)

    if ($null -eq $phone) {
        $candidates = @()
        $items = $panWindow.root.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            $listItemCondition
        )
        for ($index = 0; $index -lt $items.Count; $index++) {
            $item = $items.Item($index)
            if ($item.Current.Name -match "^iPhone") {
                $candidates += $item
            }
        }
        if ($candidates.Count -eq 1) {
            $phone = $candidates[0]
        }
    }
    if ($null -eq $phone) {
        Start-Process "$env:WINDIR\System32\DevicePairingWizard.exe"
        throw (
            "Windows has not paired with {0}; the Bluetooth pairing window was opened" `
                -f $deviceName
        )
    }

    $selection = $phone.GetCurrentPattern(
        [System.Windows.Automation.SelectionItemPattern]::Pattern
    )
    $selection.Select()
    Start-Sleep -Milliseconds 350
    $connectButton = $panWindow.root.FindFirst(
        [System.Windows.Automation.TreeScope]::Descendants,
        $connectButtonCondition
    )
    if ($null -eq $connectButton -or -not $connectButton.Current.IsEnabled) {
        throw "Windows Bluetooth access-point action is unavailable"
    }

    $beforePopups = @{}
    foreach ($handle in Get-PopupHandles) {
        $beforePopups[[string][long]$handle] = $true
    }
    $invoke = $connectButton.GetCurrentPattern(
        [System.Windows.Automation.InvokePattern]::Pattern
    )
    $invoke.Invoke()
    $popup = [System.IntPtr]::Zero
    $deadline = (Get-Date).AddSeconds(4)
    do {
        Start-Sleep -Milliseconds 100
        $popup = Find-ConnectPopup $beforePopups
    } while ($popup -eq [System.IntPtr]::Zero -and (Get-Date) -lt $deadline)
    if ($popup -eq [System.IntPtr]::Zero) {
        throw "Windows Bluetooth access-point menu did not open"
    }

    [void][MobileProfilerPanNative]::PostMessage(
        $popup,
        0x0100,
        [System.IntPtr]0x28,
        [System.IntPtr]::Zero
    )
    [void][MobileProfilerPanNative]::PostMessage(
        $popup,
        0x0101,
        [System.IntPtr]0x28,
        [System.IntPtr]::Zero
    )
    Start-Sleep -Milliseconds 200
    [void][MobileProfilerPanNative]::PostMessage(
        $popup,
        0x0100,
        [System.IntPtr]0x0D,
        [System.IntPtr]::Zero
    )
    [void][MobileProfilerPanNative]::PostMessage(
        $popup,
        0x0101,
        [System.IntPtr]0x0D,
        [System.IntPtr]::Zero
    )

    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    do {
        Start-Sleep -Milliseconds 500
        $connection = Get-PanConnection $false
    } while ($null -eq $connection -and (Get-Date) -lt $deadline)
    if ($null -eq $connection) {
        throw "Windows did not establish the iPhone Bluetooth network connection"
    }
    [Console]::Out.WriteLine(($connection | ConvertTo-Json -Compress))
    [Console]::Out.Flush()
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
finally {
    if ($openedWindow -and $null -ne $panWindow) {
        [void][MobileProfilerPanNative]::PostMessage(
            $panWindow.handle,
            0x0010,
            [System.IntPtr]::Zero,
            [System.IntPtr]::Zero
        )
    }
}
"""


def _run_windows_bluetooth_pan(
    device_name: str,
    timeout_s: float = 30.0,
) -> Dict[str, object]:
    if sys.platform != "win32":
        raise RuntimeError("iPhone Bluetooth PAN connection is supported only on Windows")
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        raise RuntimeError("Windows PowerShell was not found")
    environment = {
        **os.environ,
        "MOBILE_PROFILER_IOS_BLUETOOTH_NAME": str(device_name or "iPhone"),
        "MOBILE_PROFILER_IOS_BLUETOOTH_TIMEOUT_SECONDS": str(
            max(8, int(timeout_s))
        ),
    }
    try:
        result = _run_subprocess(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                (
                    "$source = [Console]::In.ReadToEnd(); "
                    "& ([ScriptBlock]::Create($source))"
                ),
            ],
            input=_WINDOWS_BLUETOOTH_PAN_SCRIPT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(20.0, timeout_s + 15.0),
            check=False,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Windows iPhone Bluetooth connection timed out after {timeout_s:.0f} seconds"
        ) from exc
    except (OSError, TypeError) as exc:
        raise RuntimeError(f"Windows iPhone Bluetooth connection could not start: {exc}") from exc
    if result.returncode != 0:
        message = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"Windows Bluetooth helper exited with code {result.returncode}"
        )
        raise RuntimeError(message)
    for line in reversed(result.stdout.splitlines()):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError("Windows Bluetooth helper returned no connection details")


def connect_ios_bluetooth(
    requested: Optional[str],
    ios_python: str = DEFAULT_IOS_PYTHON,
    timeout_s: float = 30.0,
) -> Dict[str, object]:
    if sys.platform != "win32":
        raise RuntimeError("iPhone Bluetooth PAN connection is supported only on Windows")
    endpoints = _load_endpoints()
    udid = ios_udid(requested)
    if not udid:
        if len(endpoints) != 1:
            raise ValueError(
                "Select an iPhone with an existing RemotePairing record before connecting Bluetooth"
            )
        udid = next(iter(endpoints))
    cached = dict(endpoints.get(udid, {}))
    try:
        port = int(cached.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not cached or not port:
        raise ValueError(
            "Create iOS RemotePairing over trusted USB before connecting the Bluetooth hotspot"
        )

    device_name = str(cached.get("model") or cached.get("name") or "iPhone").strip()
    if device_name.lower() == "iphone":
        devices, _ = list_ios_devices(ios_python)
        selected = next(
            (item for item in devices if ios_udid(item.get("serial")) == udid),
            None,
        )
        if selected and selected.get("model"):
            device_name = str(selected["model"])

    connection = _run_windows_bluetooth_pan(device_name, timeout_s)
    host = str(connection.get("gateway") or "").strip()
    address = str(connection.get("address") or "").strip()
    if not host or not address:
        raise RuntimeError("Windows Bluetooth PAN connected without a usable IPv4 address")
    if not _endpoint_reachable(host, port, timeout_s=1.5, attempts=4):
        raise RuntimeError(
            f"Bluetooth PAN connected at {address}, but iPhone RemotePairing "
            f"{host}:{port} is not reachable. Reconnect USB and create iOS "
            "RemotePairing again while the iPhone is unlocked."
        )

    endpoint_scope = _endpoint_scope(host)
    wireless_lan_candidate = _endpoint_is_unplug_candidate(host)
    adapter_name = str(connection.get("adapter_name") or "Bluetooth PAN")
    _save_endpoint(
        udid,
        host,
        port,
        cached,
        wireless_transport="bluetooth-pan",
        transport_source="bluetooth-connect",
        local_address=address,
        adapter_name=adapter_name,
    )
    return {
        "serial": ios_device_id(udid),
        "connected": True,
        "transport": "bluetooth-pan",
        "adapter": adapter_name,
        "address": address,
        "gateway": host,
        "link_speed": str(connection.get("link_speed") or ""),
        "already_connected": bool(connection.get("already_connected")),
        "endpoint": {
            "host": host,
            "port": port,
            "scope": endpoint_scope,
            "wireless_transport": "bluetooth-pan",
            "transport_label": _ios_wireless_transport_label("bluetooth-pan"),
            "transport_source": "bluetooth-connect",
            "local_address": address,
            "adapter_name": adapter_name,
            "remote_xpc_ready": True,
            "wireless_lan_candidate": wireless_lan_candidate,
            "unplug_ready": False,
        },
    }


def _device_flag(
    device: Dict[str, str],
    name: str,
    fallback: Optional[str] = None,
) -> bool:
    if name in device:
        return str(device.get(name) or "").strip().lower() == "true"
    return bool(fallback) and str(device.get(fallback) or "").strip().lower() == "true"


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
    raw_warnings = payload.get("warnings")
    discovery_warnings = (
        [str(value) for value in raw_warnings if str(value).strip()]
        if isinstance(raw_warnings, list)
        else []
    )
    endpoints = _load_endpoints()
    devices: Dict[str, Dict[str, str]] = {}
    unavailable_wireless: Dict[str, str] = {}
    for raw in raw_devices:
        if not isinstance(raw, dict) or not raw.get("udid"):
            continue
        udid = str(raw["udid"])
        cached = dict(endpoints.get(udid, {}))
        host = raw.get("host") or raw.get("wireless_host") or cached.get("host")
        port = raw.get("port") or raw.get("wireless_port") or cached.get("port")
        remote_xpc_ready = _endpoint_reachable(host, port)
        endpoint_scope = _endpoint_scope(host) if host else "unknown"
        transport_details = (
            _ios_wireless_transport_details(host, cached)
            if remote_xpc_ready
            else {
                "wireless_transport": _normalize_ios_wireless_transport(
                    cached.get("wireless_transport")
                ),
                "transport_source": "cache",
            }
        )
        if remote_xpc_ready:
            _save_endpoint(udid, host, port, raw, **transport_details)
        reported_connection = str(raw.get("connection_type") or "usb").lower()
        usb_present = reported_connection == "usb"
        unplug_ready = bool(
            remote_xpc_ready
            and not usb_present
            and _endpoint_is_unplug_candidate(host)
        )
        if not usb_present and not remote_xpc_ready:
            endpoint = f"{host}:{port}" if host and port else "no reachable endpoint"
            unavailable_wireless[udid] = endpoint
            continue
        state = str(raw.get("state") or "offline") if usb_present else "device"
        if remote_xpc_ready:
            state = "device"
        connection_type = (
            "usb"
            if usb_present
            else "wireless"
            if remote_xpc_ready
            else reported_connection
        )
        transports = ["usb"] if usb_present else []
        if remote_xpc_ready:
            transports.append("wireless" if unplug_ready else "remote-xpc")
        devices[udid] = {
            "serial": ios_device_id(udid),
            "udid": udid,
            "state": state,
            "platform": "ios",
            "connection_type": connection_type,
            "transports": ",".join(transports),
            "remote_xpc_ready": str(remote_xpc_ready).lower(),
            "wireless_ready": str(unplug_ready).lower(),
            "unplug_ready": str(unplug_ready).lower(),
            "wireless_lan_candidate": str(
                bool(remote_xpc_ready and _endpoint_is_unplug_candidate(host))
            ).lower(),
            "endpoint_scope": endpoint_scope,
            "wireless_transport": _normalize_ios_wireless_transport(
                transport_details.get("wireless_transport")
            ),
            "transport_source": str(
                transport_details.get("transport_source") or "unavailable"
            ),
            "transport_local_address": str(
                transport_details.get("local_address") or ""
            ),
            "transport_adapter": str(
                transport_details.get("adapter_name") or ""
            ),
            "transport_adapter_index": str(
                transport_details.get("adapter_index") or ""
            ),
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
        if udid in devices or udid in unavailable_wireless:
            continue
        host = cached.get("host")
        port = cached.get("port")
        if not _endpoint_reachable(host, port):
            endpoint = f"{host}:{port}" if host and port else "no reachable endpoint"
            unavailable_wireless[udid] = endpoint
            continue
        endpoint_scope = _endpoint_scope(host)
        unplug_ready = _endpoint_is_unplug_candidate(host)
        transport_details = _ios_wireless_transport_details(host, cached)
        devices[udid] = {
            "serial": ios_device_id(udid),
            "udid": udid,
            "state": "device",
            "platform": "ios",
            "connection_type": "wireless",
            "transports": "wireless" if unplug_ready else "remote-xpc",
            "remote_xpc_ready": "true",
            "wireless_ready": str(unplug_ready).lower(),
            "unplug_ready": str(unplug_ready).lower(),
            "wireless_lan_candidate": str(unplug_ready).lower(),
            "endpoint_scope": endpoint_scope,
            "wireless_transport": _normalize_ios_wireless_transport(
                transport_details.get("wireless_transport")
            ),
            "transport_source": str(
                transport_details.get("transport_source") or "unavailable"
            ),
            "transport_local_address": str(
                transport_details.get("local_address") or ""
            ),
            "transport_adapter": str(
                transport_details.get("adapter_name") or ""
            ),
            "transport_adapter_index": str(
                transport_details.get("adapter_index") or ""
            ),
            "model": str(cached.get("model") or cached.get("name") or "iPhone"),
            "product": str(cached.get("product_type") or "iOS"),
            "product_version": str(cached.get("product_version") or ""),
            "host": str(host or ""),
            "port": str(port or ""),
            "remote_paired": "true",
        }
    if not devices and unavailable_wireless:
        endpoints_text = ", ".join(
            f"{ios_device_id(udid)} ({endpoint})"
            for udid, endpoint in unavailable_wireless.items()
        )
        unavailable_error = (
            "The cached iOS RemotePairing endpoint is no longer reachable: "
            f"{endpoints_text}. Connect the iPhone by USB, keep it unlocked, "
            "and create iOS wireless pairing again."
        )
        bridge_error = " | ".join(
            value for value in (bridge_error, unavailable_error) if value
        )
    if not devices and discovery_warnings:
        bridge_error = " | ".join(
            value
            for value in (bridge_error, "; ".join(discovery_warnings))
            if value
        )
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
    host = str(endpoint.get("host") or "").strip()
    try:
        port = int(endpoint.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not host or not port:
        raise RuntimeError(
            "RemotePairing completed, but no reachable RemoteXPC endpoint was discovered "
            f"within {timeout_s:g} seconds. Keep the iPhone unlocked, confirm Wi-Fi "
            "connections are enabled, place the computer and iPhone on the same LAN, "
            "and allow Bonjour/RemotePairing through the firewall."
        )
    if not _endpoint_reachable(host, port, timeout_s=2.0):
        raise RuntimeError(
            f"RemotePairing returned {host}:{port}, but the endpoint is not reachable. "
            "Keep the iPhone unlocked, verify both devices are on the same LAN, and "
            "check VPN, access-point isolation, and firewall settings."
        )
    endpoint_scope = _endpoint_scope(host)
    wireless_lan_candidate = _endpoint_is_unplug_candidate(host)
    transport_details = _ios_wireless_transport_details(host, _load_endpoints().get(udid))
    _save_endpoint(udid, host, port, device, **transport_details)
    result["endpoint"] = {
        **endpoint,
        "host": host,
        "port": port,
        "scope": endpoint_scope,
        "wireless_transport": _normalize_ios_wireless_transport(
            transport_details.get("wireless_transport")
        ),
        "transport_label": _ios_wireless_transport_label(
            transport_details.get("wireless_transport")
        ),
        "transport_source": str(
            transport_details.get("transport_source") or "unavailable"
        ),
        "local_address": str(transport_details.get("local_address") or ""),
        "adapter_name": str(transport_details.get("adapter_name") or ""),
        "remote_xpc_ready": True,
        "wireless_lan_candidate": wireless_lan_candidate,
        "unplug_ready": False,
    }
    result["serial"] = ios_device_id(udid)
    result["connected"] = True
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
    connection = result.get("connection")
    if isinstance(connection, dict):
        wireless_transport = _normalize_ios_wireless_transport(
            device.get("wireless_transport")
        )
        connection.update(
            {
                "type": "remote-pairing",
                "transport": wireless_transport,
                "transport_label": _ios_wireless_transport_label(
                    wireless_transport
                ),
                "transport_source": str(
                    device.get("transport_source") or "unavailable"
                ),
                "local_address": str(
                    device.get("transport_local_address") or ""
                ),
                "adapter_name": str(device.get("transport_adapter") or ""),
                "adapter_index": str(
                    device.get("transport_adapter_index") or ""
                ),
                "remote_xpc_ready": _device_flag(
                    device, "remote_xpc_ready", "wireless_ready"
                ),
                "unplug_ready": _device_flag(device, "unplug_ready", "wireless_ready"),
                "wireless_lan_candidate": _device_flag(
                    device, "wireless_lan_candidate", "wireless_ready"
                ),
                "endpoint_scope": str(device.get("endpoint_scope") or "unknown"),
            }
        )
    if host and port:
        _save_endpoint(
            udid,
            host,
            port,
            result.get("device") if isinstance(result.get("device"), dict) else None,
            wireless_transport=device.get("wireless_transport"),
            transport_source=device.get("transport_source"),
            local_address=device.get("transport_local_address"),
            adapter_name=device.get("transport_adapter"),
            adapter_index=(
                int(device["transport_adapter_index"])
                if str(device.get("transport_adapter_index") or "").isdigit()
                else None
            ),
        )
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
            "iOS recording requires a cached, reachable RemotePairing endpoint; run ios-pair while USB is connected"
        )

    result = IOSCollectionResult()
    started = time.monotonic()
    unlimited = duration_s <= 0
    deadline = math.inf if unlimited else started + float(duration_s)
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
        remaining = 0.0 if unlimited else max(2.0, deadline - time.monotonic())
        command: List[str] = _bridge_command(
            ios_python,
            "record",
            "--udid",
            udid,
            "--host",
            host,
            "--port",
            port,
        )
        if unlimited:
            command.append("--unlimited")
        else:
            command.extend(["--duration", str(remaining)])
        command.extend(
            [
                "--interval",
                str(interval_s),
                "--process-interval",
                str(process_interval_s),
                "--clock-interval",
                str(checkpoint_interval_s),
            ]
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

        if returncode == 0 and not unlimited:
            result.stop_reason = "completed"
            break

        if returncode == 0 and unlimited:
            message = (
                "iOS sidecar ended before the operator stopped the unlimited recording; "
                "the RemotePairing session will be reconnected."
            )
            if message not in result.warnings:
                result.warnings.append(message)
                journal.append_stderr_line(message)

        if time.monotonic() >= deadline:
            result.stop_reason = "completed" if result.sample_count >= 2 else "collector_error"
            break
        if outage_started is None:
            outage_started = time.monotonic()
        if time.monotonic() - outage_started >= reconnect_timeout_s:
            result.stop_reason = "ios_disconnected"
            result.warnings.append(
                "iPhone RemotePairing RemoteXPC endpoint did not recover before the reconnect timeout."
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
