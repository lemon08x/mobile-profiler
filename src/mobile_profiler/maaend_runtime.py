"""Lifecycle adapter for an official MaaEnd release managed by MXU.

The adapter deliberately treats MaaEnd as an external, user-installed runtime.
It validates the v2.20 release and an MXU ADB profile, then drives the runtime
through MXU's loopback HTTP API. MaaFramework DLLs and MaaEnd agents are never
loaded into the Mobile Profiler process.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


MAAEND_REPOSITORY = "https://github.com/MaaEnd/MaaEnd"
MAAEND_LICENSE = "AGPL-3.0"
MAAEND_ADAPTER_ID = "maaend-mxu-profile"
MAAEND_CONFIG_FILENAME = "mxu-MaaEnd.json"
MAAEND_MANAGED_INSTANCE_ID = "mobile-profiler-adb"
MAAEND_MANAGED_INSTANCE_NAME = "Mobile Profiler · ADB 日常"
MAAEND_GAME_PACKAGE = "com.hypergryph.endfield"
MAAEND_STANDARD_VERSION = "v2.20.0"

_MXU_API_PORTS = tuple(range(12701, 12711))
_MXU_API_STARTUP_TIMEOUT_SECONDS = 30.0
_MXU_CONNECT_TIMEOUT_SECONDS = 45.0
_MXU_RESOURCE_TIMEOUT_SECONDS = 90.0
_MXU_SCREENSHOT_TIMEOUT_SECONDS = 20.0
_MAX_MXU_SCREENSHOT_BYTES = 32 * 1024 * 1024

# Lossless, device-generic MaaFramework methods: EncodeToFileAndPull, Encode,
# RawWithGzip; and AdbShell, MinitouchAndAdbKey, Maatouch.  Some physical
# phones expose the Androws service, causing Toolkit discovery to return only
# EmulatorExtras (64/8).  That path hard-codes display id 0 and fails on newer
# Android builds whose physical display uses a 64-bit id.  Fall back to these
# standard ADB methods whenever discovery offers no generic method.
_ADB_GENERIC_SCREENCAP_METHODS = 1 | 2 | 4
_ADB_GENERIC_INPUT_METHODS = 1 | 2 | 4

# MaaEnd v2.20's ADB resource moves the friends navigation to a 160 px side
# rail, but several upstream recognition ROIs still clip the selected friends
# icon on a real 1280x720 Android frame.  The normal list and the in-visit list
# place the same marker at different x positions, so their shared ROI must span
# both layouts.  The current game client also uses a different close glyph in
# the visitor terminal; recognize the stable title and click the verified UI
# bounds, then click the visible Leave control instead of relying on the old
# desktop-oriented key path.  Inject these narrowly scoped PI overrides after
# the upstream task options; the installed v2.20 resource files remain
# untouched.
_MAAEND_V220_ADB_TASK_OVERRIDES: dict[str, list[dict[str, object]]] = {
    "VisitFriends": [
        {
            "__ScenePrivateMenuFriendsEnterMenuFriendsList": {
                "roi": [0, 130, 160, 215]
            },
            "__ScenePrivateMenuFriendsEnterMenuFriendsListSuccess": {
                "recognition": {"param": {"roi": [0, 0, 350, 70]}}
            },
            "InFriendsList": {"roi": [75, 60, 250, 100]},
            "VisitFriendsRecognitionItemEnterButton": {"threshold": 0.75},
            "VisitFriendsMenu": {
                "recognition": {"param": {"roi": [75, 60, 250, 100]}}
            },
            "VisitFriendsMenuScanScrollList": {
                "recognition": {"param": {"roi": [75, 60, 250, 100]}}
            },
            "VisitFriendsMenuTerminalExitToWorldShip": {
                "recognition": "OCR",
                "roi": [0, 0, 220, 80],
                "expected": [
                    "访客终端",
                    "訪客終端",
                    "Guest Terminal",
                    "GUEST TERMINAL",
                    "訪問端末",
                    "방문자 단말기",
                ],
                "action": "Click",
                "target": [1135, 5, 65, 65],
            },
            "VisitFriendsWorldShipExitToMenuFriends": {
                "recognition": "TemplateMatch",
                "roi": [0, 0, 220, 100],
                "template": "VisitFriends/ShipEscButton.png",
                "action": "Click",
                "target": [70, 10, 150, 60],
            },
        }
    ]
}

# MaaEnd v2.20.0 bundles MXU 2.3.0.  Its release entry point requests UAC
# elevation before Tauri (and therefore the loopback API) is started, even
# though the ADB/API path itself does not require administrator privileges.
# Keep the signed upstream executable untouched and create a narrowly patched
# sibling host.  The complete source hash and the exact instruction bytes are
# checked before writing anything, so an unknown MaaEnd/MXU build is rejected
# instead of being modified heuristically.
_MXU_V220_EXECUTABLE_SHA256 = (
    "ce8abaddd6c52e8b0464c939a4473e52c145b8b7f78e0c239df49cadad24b7f9"
)
_MXU_V220_API_HOST_SHA256 = (
    "92641b1d08f1c30e1b4b03469ebe8b72156fc56cfb35233dfdad2ca41afa2d12"
)
_MXU_V220_ELEVATION_BRANCH_OFFSET = 0xECC7
_MXU_V220_ELEVATION_BRANCH = bytes.fromhex("0f850b010000")
_MXU_V220_RUN_BRANCH = bytes.fromhex("e90c01000090")
_MXU_API_HOST_FILENAME = "MaaEnd.mobile-profiler-api-v2.20.exe"

_MAX_JSON_BYTES = 32 * 1024 * 1024
_MAX_IMPORT_FILES = 128
_REQUIRED_FILES = (
    "MaaEnd.exe",
    "interface.json",
    "LICENSE",
    "maafw/MaaFramework.dll",
    "maafw/MaaAgentClient.dll",
    "agent/go-service.exe",
    "agent/cpp-algo.exe",
)
_REQUIRED_DIRECTORIES = (
    "resource",
    "resource_adb",
    "maafw/MaaAgentBinary",
)
_INTERFACE_COLLECTION_KEYS = (
    "task",
    "option",
    "preset",
    "group",
    "pretask",
    "exec_task",
    "global_option",
    "setting",
)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeError(f"无法读取 MaaEnd 可执行文件 {path}: {exc}") from exc
    return digest.hexdigest()


def _prepare_mxu_v220_api_host(root: Path) -> Path:
    """Create the verified non-elevating MXU 2.3.0 API host beside MaaEnd.exe."""

    root = root.resolve()
    source = (root / "MaaEnd.exe").resolve()
    target = (root / _MXU_API_HOST_FILENAME).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("MaaEnd API 宿主路径越出发布目录") from exc

    source_hash = _sha256_file(source)
    if source_hash != _MXU_V220_EXECUTABLE_SHA256:
        raise RuntimeError(
            "MaaEnd v2.20.0 的 MaaEnd.exe 指纹与已验证的 MXU 2.3.0 不一致；"
            "为避免修改未知二进制，已拒绝生成无提权 API 宿主"
        )
    if target.is_file() and _sha256_file(target) == _MXU_V220_API_HOST_SHA256:
        return target

    try:
        payload = bytearray(source.read_bytes())
    except OSError as exc:
        raise RuntimeError(f"无法读取 MaaEnd.exe 以生成 API 宿主: {exc}") from exc
    offset = _MXU_V220_ELEVATION_BRANCH_OFFSET
    end = offset + len(_MXU_V220_ELEVATION_BRANCH)
    if payload[offset:end] != _MXU_V220_ELEVATION_BRANCH:
        raise RuntimeError("MaaEnd v2.20.0 的 MXU 提权入口字节不匹配，已拒绝生成 API 宿主")
    payload[offset:end] = _MXU_V220_RUN_BRANCH
    if hashlib.sha256(payload).hexdigest() != _MXU_V220_API_HOST_SHA256:
        raise RuntimeError("MaaEnd v2.20.0 无提权 API 宿主校验失败")
    try:
        _atomic_write(target, bytes(payload))
    except OSError as exc:
        raise RuntimeError(f"无法在 MaaEnd 发布目录生成无提权 API 宿主: {exc}") from exc
    if _sha256_file(target) != _MXU_V220_API_HOST_SHA256:
        raise RuntimeError("写入后的 MaaEnd v2.20.0 API 宿主校验失败")
    return target


def _strip_jsonc_comments(value: str) -> str:
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(value):
        char = value[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < len(value):
            next_char = value[index + 1]
            if next_char == "/":
                index += 2
                while index < len(value) and value[index] not in "\r\n":
                    index += 1
                continue
            if next_char == "*":
                index += 2
                while index + 1 < len(value):
                    if value[index] == "*" and value[index + 1] == "/":
                        index += 2
                        break
                    if value[index] in "\r\n":
                        output.append(value[index])
                    index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def _strip_jsonc_trailing_commas(value: str) -> str:
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(value):
        char = value[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(value) and value[lookahead].isspace():
                lookahead += 1
            if lookahead < len(value) and value[lookahead] in "]}":
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def _load_jsonc(path: Path) -> dict[str, object]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"无法读取配置文件 {path}: {exc}") from exc
    if size > _MAX_JSON_BYTES:
        raise RuntimeError(f"配置文件过大: {path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"无法读取 UTF-8 配置文件 {path}: {exc}") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        normalized = _strip_jsonc_trailing_commas(_strip_jsonc_comments(text))
        try:
            value = json.loads(normalized)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"配置文件不是有效 JSON/JSONC: {path} ({exc.msg}, line {exc.lineno})"
            ) from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"配置文件根节点必须是对象: {path}")
    return value


def _resolve_import(root: Path, parent: Path, raw_path: object) -> Path:
    relative = str(raw_path or "").strip()
    if not relative:
        raise RuntimeError(f"interface import 路径为空: {parent}")
    candidate = (parent.parent / relative).resolve()
    if not candidate.is_relative_to(root):
        raise RuntimeError(f"interface import 越出 MaaEnd 目录: {relative}")
    if candidate.suffix.lower() not in {".json", ".jsonc"} or not candidate.is_file():
        raise RuntimeError(f"interface import 文件不存在或类型不受支持: {relative}")
    return candidate


def _load_interface_bundle(root: Path) -> dict[str, object]:
    root = root.resolve()
    entry = root / "interface.json"
    seen: set[Path] = set()
    documents: list[dict[str, object]] = []

    def visit(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        if len(seen) >= _MAX_IMPORT_FILES:
            raise RuntimeError("interface import 文件数量超过安全上限")
        seen.add(resolved)
        document = _load_jsonc(resolved)
        documents.append(document)
        imports = document.get("import", [])
        if imports is None:
            return
        if not isinstance(imports, list):
            raise RuntimeError(f"interface import 必须是数组: {resolved}")
        for raw_import in imports:
            visit(_resolve_import(root, resolved, raw_import))

    visit(entry)
    interface = dict(documents[0])
    for key in _INTERFACE_COLLECTION_KEYS:
        values = [document.get(key) for document in documents if key in document]
        if not values:
            continue
        if all(isinstance(value, list) for value in values):
            interface[key] = [
                item
                for value in values
                for item in value  # type: ignore[union-attr]
            ]
        elif all(isinstance(value, dict) for value in values):
            merged: dict[str, object] = {}
            for value in values:
                merged.update(value)  # type: ignore[arg-type]
            interface[key] = merged
        else:
            raise RuntimeError(f"interface 字段 {key} 在导入文件中的类型不一致")
    return interface


def _directory_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    except OSError:
        return total
    return total


def _tail_text_file(path: Path, *, maximum_bytes: int = 128 * 1024) -> dict[str, object]:
    if not path.is_file():
        return {"path": str(path), "available": False, "lines": []}
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > maximum_bytes:
                handle.seek(size - maximum_bytes)
            payload = handle.read(maximum_bytes)
    except OSError as exc:
        return {
            "path": str(path),
            "available": False,
            "lines": [],
            "error": str(exc),
        }
    text = payload.decode("utf-8", errors="replace")
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return {
        "path": str(path),
        "available": True,
        "size": size,
        "truncated": size > maximum_bytes,
        "lines": lines[-80:],
    }


def validate_maaend_runtime(path: Path | str) -> dict[str, object]:
    """Validate a complete official Windows MaaEnd release directory."""

    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"MaaEnd 发布目录不存在: {root}")
    for relative in _REQUIRED_FILES:
        candidate = root / relative
        if not candidate.is_file():
            raise RuntimeError(f"MaaEnd 发布包缺少文件: {relative}")
        if not candidate.resolve().is_relative_to(root):
            raise RuntimeError(f"MaaEnd 发布包文件越出根目录: {relative}")
    for relative in _REQUIRED_DIRECTORIES:
        candidate = root / relative
        if not candidate.is_dir():
            raise RuntimeError(f"MaaEnd 发布包缺少目录: {relative}")
        if not candidate.resolve().is_relative_to(root):
            raise RuntimeError(f"MaaEnd 发布包目录越出根目录: {relative}")

    license_path = root / "LICENSE"
    if license_path.stat().st_size > 2 * 1024 * 1024:
        raise RuntimeError("MaaEnd LICENSE 文件异常过大")
    try:
        license_text = license_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"无法读取 MaaEnd LICENSE: {exc}") from exc
    normalized_license = license_text.upper()
    if (
        "GNU AFFERO GENERAL PUBLIC LICENSE" not in normalized_license
        or "VERSION 3" not in normalized_license
    ):
        raise RuntimeError("MaaEnd LICENSE 不是预期的 AGPL-3.0 文本")

    interface = _load_interface_bundle(root)
    if interface.get("interface_version") != 2:
        raise RuntimeError("MaaEnd interface_version 必须为 2")
    if str(interface.get("name") or "").strip() != "MaaEnd":
        raise RuntimeError("interface.json 不是 MaaEnd 项目接口")
    version = str(interface.get("version") or "").strip()
    if version != MAAEND_STANDARD_VERSION:
        raise RuntimeError(
            f"MaaEnd 版本必须为 {MAAEND_STANDARD_VERSION}，当前为 {version or '未知版本'}"
        )
    repository = str(interface.get("github") or "").strip().rstrip("/")
    if repository.lower() != MAAEND_REPOSITORY.lower():
        raise RuntimeError("interface.json 的上游仓库不是官方 MaaEnd/MaaEnd")

    controllers = interface.get("controller")
    if not isinstance(controllers, list) or not any(
        isinstance(item, dict)
        and str(item.get("name") or "") == "ADB"
        and str(item.get("type") or "").lower() == "adb"
        for item in controllers
    ):
        raise RuntimeError("MaaEnd interface.json 未声明 ADB 控制器")
    tasks = interface.get("task")
    if not isinstance(tasks, list) or not tasks:
        raise RuntimeError("MaaEnd interface.json 未包含任务定义")
    task_rows = [item for item in tasks if isinstance(item, dict)]
    adb_task_count = sum(
        1
        for item in task_rows
        if isinstance(item.get("controller"), list)
        and "ADB" in item["controller"]
    )
    return {
        "path": str(root),
        "repository": MAAEND_REPOSITORY,
        "license": MAAEND_LICENSE,
        "version": version,
        "interface_version": 2,
        "task_count": len(task_rows),
        "adb_task_count": adb_task_count,
        "config_path": str(root / "config" / MAAEND_CONFIG_FILENAME),
    }


def load_maaend_profile_config(root: Path | str) -> dict[str, object]:
    """Load the portable MXU configuration shipped beside MaaEnd.exe."""

    path = Path(root).expanduser().resolve() / "config" / MAAEND_CONFIG_FILENAME
    if not path.is_file():
        raise RuntimeError(f"未找到 MaaEnd 的 MXU 配置: {path}")
    config = _load_jsonc(path)
    instances = config.get("instances")
    if not isinstance(instances, list):
        raise RuntimeError(f"MXU 配置 instances 必须是数组: {path}")
    return config


def _localized_text(
    value: object,
    translations: dict[str, object],
    *,
    maximum: int,
) -> str:
    text = str(value or "").strip()
    if text.startswith("$"):
        translated = translations.get(text[1:])
        if isinstance(translated, str):
            text = translated.strip()
        else:
            text = text[1:]
    return text.replace("\x00", "")[:maximum]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item).strip()
        for item in value
        if str(item).strip()
    ]


def _option_type(definition: dict[str, object]) -> str:
    value = str(definition.get("type") or "select").strip().lower()
    return value if value in {"select", "checkbox", "switch", "input", "hotkey"} else "select"


def _definition_compatible(
    definition: dict[str, object],
    *,
    controller: str,
    resource: str,
) -> bool:
    controllers = _string_list(definition.get("controller"))
    resources = _string_list(definition.get("resource"))
    return (
        (not controllers or controller in controllers)
        and (not resources or resource in resources)
    )


def _option_default_value(definition: dict[str, object]) -> dict[str, object]:
    kind = _option_type(definition)
    if kind in {"input", "hotkey"}:
        key = "hotkeys" if kind == "hotkey" else "inputs"
        fields = definition.get(key)
        values = {
            str(field.get("name") or "").strip(): str(field.get("default") or "")
            for field in fields
            if isinstance(field, dict) and str(field.get("name") or "").strip()
        } if isinstance(fields, list) else {}
        return {"type": kind, "values": values}
    if kind == "checkbox":
        return {
            "type": "checkbox",
            "caseNames": _string_list(definition.get("default_case")),
        }
    cases = definition.get("cases")
    first_case = next(
        (
            str(item.get("name") or "").strip()
            for item in cases
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ),
        "",
    ) if isinstance(cases, list) else ""
    default_case = str(definition.get("default_case") or first_case).strip()
    if kind == "switch":
        return {
            "type": "switch",
            "value": default_case in {"Yes", "yes", "Y", "y"},
        }
    return {"type": "select", "caseName": default_case}


def _selected_option_case(
    definition: dict[str, object],
    value: dict[str, object],
) -> Optional[dict[str, object]]:
    cases = definition.get("cases")
    if not isinstance(cases, list):
        return None
    if value.get("type") == "switch":
        wanted = (
            {"Yes", "yes", "Y", "y"}
            if value.get("value") is True
            else {"No", "no", "N", "n"}
        )
        return next(
            (
                item
                for item in cases
                if isinstance(item, dict)
                and str(item.get("name") or "") in wanted
            ),
            None,
        )
    case_name = str(value.get("caseName") or "")
    return next(
        (
            item
            for item in cases
            if isinstance(item, dict)
            and str(item.get("name") or "") == case_name
        ),
        None,
    )


def _initialize_option_values(
    option_ids: list[str],
    definitions: dict[str, dict[str, object]],
    result: Optional[dict[str, dict[str, object]]] = None,
) -> dict[str, dict[str, object]]:
    values = result if result is not None else {}
    for option_id in option_ids:
        definition = definitions.get(option_id)
        if definition is None or option_id in values:
            continue
        value = _option_default_value(definition)
        values[option_id] = value
        if _option_type(definition) not in {"select", "switch"}:
            continue
        selected = _selected_option_case(definition, value)
        if selected is not None:
            _initialize_option_values(
                _string_list(selected.get("option")),
                definitions,
                values,
            )
    return values


def _reachable_option_ids(
    option_ids: list[str],
    definitions: dict[str, dict[str, object]],
) -> set[str]:
    reachable: set[str] = set()
    pending = list(option_ids)
    while pending:
        option_id = pending.pop()
        if option_id in reachable:
            continue
        definition = definitions.get(option_id)
        if definition is None:
            continue
        reachable.add(option_id)
        cases = definition.get("cases")
        if isinstance(cases, list):
            for case in cases:
                if isinstance(case, dict):
                    pending.extend(_string_list(case.get("option")))
    return reachable


def _normalize_option_value(
    option_id: str,
    raw_value: object,
    definition: dict[str, object],
) -> dict[str, object]:
    if not isinstance(raw_value, dict):
        raise ValueError(f"MaaEnd 选项 {option_id} 的值必须是对象")
    kind = _option_type(definition)
    if str(raw_value.get("type") or "") != kind:
        raise ValueError(f"MaaEnd 选项 {option_id} 的类型必须是 {kind}")
    if kind in {"select", "checkbox"}:
        cases = definition.get("cases")
        case_names = {
            str(item.get("name") or "").strip()
            for item in cases
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        } if isinstance(cases, list) else set()
        if kind == "select":
            selected = str(raw_value.get("caseName") or "").strip()
            if selected not in case_names:
                raise ValueError(f"MaaEnd 选项 {option_id} 的 case 无效: {selected}")
            return {"type": "select", "caseName": selected}
        selected_rows = _string_list(raw_value.get("caseNames"))
        invalid = [item for item in selected_rows if item not in case_names]
        if invalid:
            raise ValueError(
                f"MaaEnd 选项 {option_id} 包含无效 case: {', '.join(invalid)}"
            )
        return {"type": "checkbox", "caseNames": selected_rows}
    if kind == "switch":
        if not isinstance(raw_value.get("value"), bool):
            raise ValueError(f"MaaEnd 选项 {option_id} 的 value 必须是布尔值")
        return {"type": "switch", "value": raw_value["value"]}

    key = "hotkeys" if kind == "hotkey" else "inputs"
    fields = definition.get(key)
    field_names = [
        str(item.get("name") or "").strip()
        for item in fields
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ] if isinstance(fields, list) else []
    raw_values = raw_value.get("values")
    if not isinstance(raw_values, dict):
        raise ValueError(f"MaaEnd 选项 {option_id} 的 values 必须是对象")
    unknown = [str(item) for item in raw_values if str(item) not in field_names]
    if unknown:
        raise ValueError(
            f"MaaEnd 选项 {option_id} 包含未知输入字段: {', '.join(unknown)}"
        )
    defaults = _option_default_value(definition)["values"]
    normalized: dict[str, str] = {}
    for field_name in field_names:
        field_value = str(raw_values.get(field_name, defaults.get(field_name, "")))
        if len(field_value) > 4096 or "\x00" in field_value:
            raise ValueError(f"MaaEnd 选项 {option_id}.{field_name} 的值无效")
        normalized[field_name] = field_value
    return {"type": kind, "values": normalized}


def _normalize_task_option_values(
    task_definition: dict[str, object],
    raw_values: object,
    option_definitions: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    option_ids = _string_list(task_definition.get("option"))
    reachable = _reachable_option_ids(option_ids, option_definitions)
    if raw_values is None:
        raw_values = {}
    if not isinstance(raw_values, dict):
        raise ValueError(
            f"MaaEnd 任务 {task_definition.get('name') or '<unknown>'} 的 optionValues 必须是对象"
        )
    unknown = [str(item) for item in raw_values if str(item) not in reachable]
    if unknown:
        raise ValueError(
            f"MaaEnd 任务 {task_definition.get('name') or '<unknown>'} 包含未知选项: "
            + ", ".join(unknown)
        )
    normalized = _initialize_option_values(option_ids, option_definitions)
    for option_id, value in raw_values.items():
        definition = option_definitions[str(option_id)]
        normalized[str(option_id)] = _normalize_option_value(
            str(option_id),
            value,
            definition,
        )
    # A changed select/switch may expose a nested branch which was not part of
    # the original defaults. Initialize that branch exactly as MXU does when a
    # user changes the parent option in its editor.
    _initialize_option_values(option_ids, option_definitions, normalized)
    for option_id in list(normalized):
        definition = option_definitions.get(option_id)
        if definition is None or _option_type(definition) not in {"select", "switch"}:
            continue
        selected = _selected_option_case(definition, normalized[option_id])
        if selected is not None:
            _initialize_option_values(
                _string_list(selected.get("option")),
                option_definitions,
                normalized,
            )
    return normalized


def _convert_preset_option_value(
    option_id: str,
    raw_value: object,
    definition: dict[str, object],
) -> Optional[dict[str, object]]:
    kind = _option_type(definition)
    if kind == "switch" and isinstance(raw_value, str):
        return {
            "type": "switch",
            "value": raw_value in {"Yes", "yes", "Y", "y"},
        }
    if kind == "checkbox" and isinstance(raw_value, list):
        return _normalize_option_value(
            option_id,
            {"type": "checkbox", "caseNames": raw_value},
            definition,
        )
    if kind in {"input", "hotkey"} and isinstance(raw_value, dict):
        return _normalize_option_value(
            option_id,
            {"type": kind, "values": raw_value},
            definition,
        )
    if kind == "select" and isinstance(raw_value, str):
        return _normalize_option_value(
            option_id,
            {"type": "select", "caseName": raw_value},
            definition,
        )
    return None


def _replace_pipeline_placeholders(
    value: object,
    replacements: dict[str, object],
) -> object:
    if isinstance(value, dict):
        return {
            str(key): _replace_pipeline_placeholders(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _replace_pipeline_placeholders(item, replacements)
            for item in value
        ]
    if not isinstance(value, str):
        return value
    if value in replacements:
        return replacements[value]
    result = value
    for placeholder, replacement in replacements.items():
        result = result.replace(placeholder, str(replacement))
    return result


def _adb_hotkey_code(value: str) -> int:
    key = str(value or "").strip().upper()
    aliases = {
        "BACKSPACE": 67,
        "TAB": 61,
        "ENTER": 66,
        "SHIFT": 59,
        "CTRL": 113,
        "CONTROL": 113,
        "ALT": 57,
        "ESC": 111,
        "ESCAPE": 111,
        "SPACE": 62,
        "PAGEUP": 92,
        "PAGEDOWN": 93,
        "HOME": 3,
        "LEFT": 21,
        "UP": 19,
        "RIGHT": 22,
        "DOWN": 20,
        "INSERT": 124,
        "DELETE": 112,
    }
    if key in aliases:
        return aliases[key]
    if len(key) == 1 and "0" <= key <= "9":
        return 7 + ord(key) - ord("0")
    if len(key) == 1 and "A" <= key <= "Z":
        return 29 + ord(key) - ord("A")
    try:
        return int(key)
    except ValueError:
        return 0


def _input_pipeline_override(
    definition: dict[str, object],
    value: dict[str, object],
) -> Optional[dict[str, object]]:
    override = definition.get("pipeline_override")
    if not isinstance(override, dict):
        return None
    kind = _option_type(definition)
    key = "hotkeys" if kind == "hotkey" else "inputs"
    fields = definition.get(key)
    values = value.get("values")
    raw_values = values if isinstance(values, dict) else {}
    replacements: dict[str, object] = {}
    if not isinstance(fields, list):
        fields = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "").strip()
        if not name:
            continue
        raw_value = str(raw_values.get(name, field.get("default") or ""))
        if kind == "hotkey":
            parts = [item.strip() for item in raw_value.split("+") if item.strip()]
            primary = parts[-1] if parts else ""
            modifiers = parts[:-1]
            replacements[f"{{{name}}}"] = _adb_hotkey_code(primary)
            replacements[f"{{{name}.primary}}"] = _adb_hotkey_code(primary)
            replacements[f"{{{name}.modifier1}}"] = _adb_hotkey_code(
                modifiers[0] if modifiers else ""
            )
            replacements[f"{{{name}.modifier2}}"] = _adb_hotkey_code(
                modifiers[1] if len(modifiers) > 1 else ""
            )
            continue
        pipeline_type = str(field.get("pipeline_type") or "string")
        if pipeline_type == "int":
            try:
                replacement: object = int(raw_value or "0")
            except ValueError:
                replacement = 0
        elif pipeline_type == "bool":
            replacement = raw_value.lower() in {"true", "1", "yes", "y"}
        else:
            replacement = raw_value
        replacements[f"{{{name}}}"] = replacement
    replaced = _replace_pipeline_placeholders(override, replacements)
    return replaced if isinstance(replaced, dict) else None


def _collect_option_pipeline_overrides(
    option_id: str,
    values: dict[str, object],
    definitions: dict[str, dict[str, object]],
    output: list[dict[str, object]],
    *,
    controller: str,
    resource: str,
    trail: Optional[set[str]] = None,
) -> None:
    visited = set(trail or ())
    if option_id in visited:
        return
    visited.add(option_id)
    definition = definitions.get(option_id)
    if definition is None or not _definition_compatible(
        definition,
        controller=controller,
        resource=resource,
    ):
        return
    raw_value = values.get(option_id)
    value = (
        raw_value
        if isinstance(raw_value, dict)
        else _option_default_value(definition)
    )
    kind = _option_type(definition)
    cases = definition.get("cases")
    if kind == "checkbox" and isinstance(cases, list):
        selected = set(_string_list(value.get("caseNames")))
        for case in cases:
            if not isinstance(case, dict) or str(case.get("name") or "") not in selected:
                continue
            override = case.get("pipeline_override")
            if isinstance(override, dict):
                output.append(json.loads(json.dumps(override)))
        return
    if kind in {"select", "switch"}:
        selected_case = _selected_option_case(definition, value)
        if selected_case is None:
            return
        override = selected_case.get("pipeline_override")
        if isinstance(override, dict):
            output.append(json.loads(json.dumps(override)))
        for child_id in _string_list(selected_case.get("option")):
            _collect_option_pipeline_overrides(
                child_id,
                values,
                definitions,
                output,
                controller=controller,
                resource=resource,
                trail=visited,
            )
        return
    if kind in {"input", "hotkey"}:
        override = _input_pipeline_override(definition, value)
        if override is not None:
            output.append(override)


def _task_pipeline_override(
    interface: dict[str, object],
    task_definition: dict[str, object],
    option_values: dict[str, object],
    global_values: dict[str, object],
    *,
    controller_name: str,
    resource_name: str,
) -> str:
    raw_options = interface.get("option")
    definitions = {
        str(option_id): definition
        for option_id, definition in raw_options.items()
        if isinstance(definition, dict)
    } if isinstance(raw_options, dict) else {}
    overrides: list[dict[str, object]] = []
    task_override = task_definition.get("pipeline_override")
    if isinstance(task_override, dict):
        overrides.append(json.loads(json.dumps(task_override)))

    for option_id in _string_list(interface.get("global_option")):
        _collect_option_pipeline_overrides(
            option_id,
            global_values,
            definitions,
            overrides,
            controller=controller_name,
            resource=resource_name,
        )
    resources = interface.get("resource")
    resource = next(
        (
            item
            for item in resources
            if isinstance(item, dict)
            and str(item.get("name") or "") == resource_name
        ),
        None,
    ) if isinstance(resources, list) else None
    if isinstance(resource, dict):
        for option_id in _string_list(resource.get("option")):
            _collect_option_pipeline_overrides(
                option_id,
                option_values,
                definitions,
                overrides,
                controller=controller_name,
                resource=resource_name,
            )
    controllers = interface.get("controller")
    controller = next(
        (
            item
            for item in controllers
            if isinstance(item, dict)
            and str(item.get("name") or "") == controller_name
        ),
        None,
    ) if isinstance(controllers, list) else None
    if isinstance(controller, dict):
        for option_id in _string_list(controller.get("option")):
            _collect_option_pipeline_overrides(
                option_id,
                option_values,
                definitions,
                overrides,
                controller=controller_name,
                resource=resource_name,
            )
    for option_id in _string_list(task_definition.get("option")):
        _collect_option_pipeline_overrides(
            option_id,
            option_values,
            definitions,
            overrides,
            controller=controller_name,
            resource=resource_name,
        )
    task_name = str(task_definition.get("name") or "").strip()
    compatibility_overrides = _MAAEND_V220_ADB_TASK_OVERRIDES.get(task_name, [])
    overrides.extend(json.loads(json.dumps(compatibility_overrides)))
    return json.dumps(overrides, ensure_ascii=False, separators=(",", ":"))


def _mxu_http_json(
    base_url: str,
    method: str,
    path: str,
    payload: object = None,
    *,
    timeout: float = 15.0,
) -> object:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(
        f"{base_url}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"MXU API {method} {path} 返回 HTTP {exc.code}: {detail}"
        ) from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"MXU API {method} {path} 调用失败: {exc}") from exc
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"MXU API {method} {path} 未返回有效 JSON") from exc


def _mxu_http_bytes(
    base_url: str,
    method: str,
    path: str,
    *,
    timeout: float = 15.0,
) -> bytes:
    request = Request(
        f"{base_url}{path}",
        headers={"Accept": "image/png, application/octet-stream"},
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(_MAX_MXU_SCREENSHOT_BYTES + 1)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"MXU API {method} {path} 返回 HTTP {exc.code}: {detail}"
        ) from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"MXU API {method} {path} 调用失败: {exc}") from exc
    if len(body) > _MAX_MXU_SCREENSHOT_BYTES:
        raise RuntimeError("MXU API 控制器截图超过 32 MiB 限制")
    return body


def _named_interface_definition(
    interface: dict[str, object],
    collection: str,
    name: str,
) -> dict[str, object]:
    rows = interface.get(collection)
    definition = next(
        (
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("name") or "") == name
        ),
        None,
    ) if isinstance(rows, list) else None
    if not isinstance(definition, dict):
        raise RuntimeError(f"MaaEnd interface.json 缺少 {collection} 定义: {name}")
    return definition


def _runtime_resource_paths(
    root: Path,
    interface: dict[str, object],
    *,
    controller_name: str,
    resource_name: str,
) -> list[str]:
    controller = _named_interface_definition(
        interface,
        "controller",
        controller_name,
    )
    resource = _named_interface_definition(interface, "resource", resource_name)
    raw_paths = [
        *_string_list(resource.get("path")),
        *_string_list(controller.get("attach_resource_path")),
    ]
    if not raw_paths:
        raise RuntimeError("MaaEnd ADB 资源路径为空")
    paths: list[str] = []
    for raw_path in raw_paths:
        candidate = Path(raw_path)
        resolved = (
            candidate.expanduser().resolve()
            if candidate.is_absolute()
            else (root / candidate).resolve()
        )
        if not resolved.is_relative_to(root):
            raise RuntimeError(f"MaaEnd 资源路径越出发布目录: {raw_path}")
        if not resolved.is_dir():
            raise RuntimeError(f"MaaEnd 资源目录不存在: {resolved}")
        normalized = str(resolved).replace("\\", "/")
        if normalized not in paths:
            paths.append(normalized)
    return paths


def _mxu_agent_configs(interface: dict[str, object]) -> list[dict[str, object]]:
    raw_agents = interface.get("agent")
    if raw_agents is None:
        return []
    rows = raw_agents if isinstance(raw_agents, list) else [raw_agents]
    configs: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise RuntimeError(f"MaaEnd agent #{index + 1} 配置必须是对象")
        child_exec = str(row.get("child_exec") or "").strip()
        if not child_exec:
            raise RuntimeError(f"MaaEnd agent #{index + 1} 缺少 child_exec")
        raw_args = row.get("child_args")
        if raw_args is None:
            child_args: list[str] = []
        elif isinstance(raw_args, list) and all(
            isinstance(item, str) for item in raw_args
        ):
            child_args = list(raw_args)
        else:
            raise RuntimeError(f"MaaEnd agent #{index + 1} 的 child_args 必须是字符串数组")
        config: dict[str, object] = {
            "child_exec": child_exec,
            "child_args": child_args,
        }
        identifier = str(row.get("identifier") or "").strip()
        if identifier:
            config["identifier"] = identifier
        timeout = row.get("timeout")
        if isinstance(timeout, int) and not isinstance(timeout, bool):
            config["timeout"] = timeout
        configs.append(config)
    return configs


def _mxu_pi_envs(
    interface: dict[str, object],
    *,
    controller_name: str,
    resource_name: str,
    maafw_version: str,
) -> dict[str, str]:
    controller = _named_interface_definition(
        interface,
        "controller",
        controller_name,
    )
    resource = _named_interface_definition(interface, "resource", resource_name)
    values = {
        "PI_INTERFACE_VERSION": "v2.5.0",
        "PI_CLIENT_NAME": "MXU",
        "PI_CLIENT_VERSION": "mobile-profiler-api",
        "PI_CLIENT_LANGUAGE": "zh_cn",
        "PI_VERSION": str(interface.get("version") or ""),
        "PI_CONTROLLER": json.dumps(
            controller,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "PI_RESOURCE": json.dumps(
            resource,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    if maafw_version:
        values["PI_CLIENT_MAAFW_VERSION"] = (
            maafw_version
            if maafw_version.startswith("v")
            else f"v{maafw_version}"
        )
    return values


def _mxu_task_requests(
    interface: dict[str, object],
    config: dict[str, object],
    profile: dict[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    raw_tasks = interface.get("task")
    definitions = {
        str(row.get("name") or "").strip(): row
        for row in raw_tasks
        if isinstance(row, dict) and str(row.get("name") or "").strip()
    } if isinstance(raw_tasks, list) else {}
    global_values = config.get("globalOptionValues")
    if not isinstance(global_values, dict):
        global_values = {}
    resource_name = str(profile.get("resource") or "")
    configurations = profile.get("task_configurations")
    rows = configurations if isinstance(configurations, list) else []
    requests: list[dict[str, object]] = []
    metadata: list[dict[str, object]] = []
    selected_ids: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("enabled") is not True:
            continue
        task_name = str(row.get("name") or "").strip()
        definition = definitions.get(task_name)
        if definition is None:
            raise RuntimeError(f"MaaEnd 任务定义不存在: {task_name}")
        entry = str(definition.get("entry") or "").strip()
        if not entry:
            raise RuntimeError(f"MaaEnd 任务 {task_name} 缺少 entry")
        selected_id = str(row.get("id") or "").strip() or _stable_task_id(task_name)
        if selected_id in selected_ids:
            selected_id = f"{selected_id}-{index + 1}"
        selected_ids.add(selected_id)
        option_values = row.get("option_values")
        if not isinstance(option_values, dict):
            option_values = {}
        requests.append(
            {
                "entry": entry,
                "pipeline_override": _task_pipeline_override(
                    interface,
                    definition,
                    option_values,
                    global_values,
                    controller_name="ADB",
                    resource_name=resource_name,
                ),
                "selected_task_id": selected_id,
            }
        )
        metadata.append(
            {
                "id": selected_id,
                "name": task_name,
                "entry": entry,
                "status": "pending",
                "maa_task_id": None,
            }
        )
    if not requests:
        raise RuntimeError("MaaEnd 实例没有可提交的 ADB 任务")
    return requests, metadata


def load_maaend_game_catalog(root: Path | str) -> dict[str, object]:
    """Read MaaEnd's game-facing task catalog from its installed PI files.

    Labels and descriptions stay in the user-installed AGPL runtime instead of
    being copied into Mobile Profiler. The model mirrors MXU option defaults and
    preset conversion, while marking definitions that are usable by ADB.
    """

    runtime_root = Path(root).expanduser().resolve()
    interface = _load_interface_bundle(runtime_root)
    translations: dict[str, object] = {}
    languages = interface.get("languages")
    if isinstance(languages, dict):
        locale_path = str(languages.get("zh_cn") or "").strip()
        if locale_path:
            resolved_locale = _resolve_import(
                runtime_root,
                runtime_root / "interface.json",
                locale_path,
            )
            translations = _load_jsonc(resolved_locale)

    group_rows = interface.get("group")
    groups: list[dict[str, object]] = []
    group_ids: set[str] = set()
    if isinstance(group_rows, list):
        for index, row in enumerate(group_rows):
            if not isinstance(row, dict):
                continue
            group_id = str(row.get("name") or "").strip()
            if not group_id or group_id in group_ids:
                continue
            group_ids.add(group_id)
            groups.append(
                {
                    "id": group_id,
                    "name": _localized_text(
                        row.get("label") or group_id,
                        translations,
                        maximum=80,
                    ),
                    "order": index,
                    "default_expand": row.get("default_expand") is True,
                }
            )

    raw_resource_rows = interface.get("resource")
    resource_name = next(
        (
            str(row.get("name") or "").strip()
            for row in raw_resource_rows
            if isinstance(row, dict) and str(row.get("name") or "").strip()
        ),
        "",
    ) if isinstance(raw_resource_rows, list) else ""

    raw_option_rows = interface.get("option")
    option_definitions = {
        str(option_id).strip(): definition
        for option_id, definition in raw_option_rows.items()
        if str(option_id).strip() and isinstance(definition, dict)
    } if isinstance(raw_option_rows, dict) else {}

    task_rows = interface.get("task")
    raw_task_definitions = {
        str(row.get("name") or "").strip(): row
        for row in task_rows
        if isinstance(row, dict) and str(row.get("name") or "").strip()
    } if isinstance(task_rows, list) else {}
    adb_reachable_options: set[str] = set()
    for row in raw_task_definitions.values():
        if "ADB" in _string_list(row.get("controller")):
            adb_reachable_options.update(
                _reachable_option_ids(
                    _string_list(row.get("option")),
                    option_definitions,
                )
            )

    options: dict[str, dict[str, object]] = {}
    for option_id, definition in option_definitions.items():
        if option_id not in adb_reachable_options:
            continue
        kind = _option_type(definition)
        compatible = _definition_compatible(
            definition,
            controller="ADB",
            resource=resource_name,
        )
        public: dict[str, object] = {
            "id": option_id,
            "name": option_id,
            "type": kind,
            "label": _localized_text(
                definition.get("label") or option_id,
                translations,
                maximum=160,
            ),
            "description": _localized_text(
                definition.get("description"),
                translations,
                maximum=1200,
            ),
            "controllers": _string_list(definition.get("controller")),
            "resources": _string_list(definition.get("resource")),
            "adb_applicable": compatible,
            "default_value": _option_default_value(definition),
        }
        cases = definition.get("cases")
        if isinstance(cases, list):
            public["cases"] = [
                {
                    "name": str(case.get("name") or "").strip(),
                    "label": _localized_text(
                        case.get("label") or case.get("name"),
                        translations,
                        maximum=160,
                    ),
                    "description": _localized_text(
                        case.get("description"),
                        translations,
                        maximum=800,
                    ),
                    "option_ids": [
                        child_id
                        for child_id in _string_list(case.get("option"))
                        if child_id in adb_reachable_options
                        and _definition_compatible(
                            option_definitions.get(child_id, {}),
                            controller="ADB",
                            resource=resource_name,
                        )
                    ],
                }
                for case in cases
                if isinstance(case, dict) and str(case.get("name") or "").strip()
            ]
        field_key = "hotkeys" if kind == "hotkey" else "inputs"
        fields = definition.get(field_key)
        if isinstance(fields, list):
            public["fields"] = [
                {
                    "name": str(field.get("name") or "").strip(),
                    "label": _localized_text(
                        field.get("label") or field.get("name"),
                        translations,
                        maximum=160,
                    ),
                    "description": _localized_text(
                        field.get("description"),
                        translations,
                        maximum=800,
                    ),
                    "default": str(field.get("default") or ""),
                    "pipeline_type": str(field.get("pipeline_type") or "string"),
                    "input_type": str(field.get("input_type") or "text"),
                    "placeholder": _localized_text(
                        field.get("placeholder"),
                        translations,
                        maximum=240,
                    ),
                    "verify": str(field.get("verify") or "")[:500],
                    "pattern_message": _localized_text(
                        field.get("pattern_msg"),
                        translations,
                        maximum=500,
                    ),
                }
                for field in fields
                if isinstance(field, dict) and str(field.get("name") or "").strip()
            ]
        options[option_id] = public

    tasks: list[dict[str, object]] = []
    task_by_name: dict[str, dict[str, object]] = {}
    if isinstance(task_rows, list):
        for index, row in enumerate(task_rows):
            if not isinstance(row, dict):
                continue
            task_name = str(row.get("name") or "").strip()
            if not task_name or task_name in task_by_name:
                continue
            controllers = _string_list(row.get("controller"))
            task_groups = _string_list(row.get("group"))
            all_option_ids = _string_list(row.get("option"))
            option_ids = [
                option_id
                for option_id in all_option_ids
                if options.get(option_id, {}).get("adb_applicable") is True
            ]
            all_defaults = _initialize_option_values(
                all_option_ids,
                option_definitions,
            )
            reachable = _reachable_option_ids(all_option_ids, option_definitions)
            task = {
                "id": task_name,
                "name": task_name,
                "label": _localized_text(
                    row.get("label") or task_name,
                    translations,
                    maximum=120,
                ),
                "description": _localized_text(
                    row.get("description"),
                    translations,
                    maximum=1200,
                ),
                "groups": task_groups,
                "primary_group": task_groups[0] if task_groups else "other_menu",
                "controllers": controllers,
                "adb_supported": "ADB" in controllers,
                "default_check": row.get("default_check") is True,
                "option_ids": option_ids,
                "option_count": len(option_ids),
                "default_option_values": {
                    option_id: value
                    for option_id, value in all_defaults.items()
                    if option_id in reachable
                    and options.get(option_id, {}).get("adb_applicable") is True
                },
                "order": index,
            }
            tasks.append(task)
            task_by_name[task_name] = task

    preset_rows = interface.get("preset")
    presets: list[dict[str, object]] = []
    if isinstance(preset_rows, list):
        for index, row in enumerate(preset_rows):
            if not isinstance(row, dict):
                continue
            preset_name = str(row.get("name") or "").strip()
            raw_tasks = row.get("task")
            task_names = [
                str(item.get("name") or "").strip()
                for item in raw_tasks
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ] if isinstance(raw_tasks, list) else []
            unsupported = [
                name
                for name in task_names
                if not task_by_name.get(name, {}).get("adb_supported")
            ]
            adb_task_configurations: list[dict[str, object]] = []
            if isinstance(raw_tasks, list):
                for preset_task in raw_tasks:
                    if not isinstance(preset_task, dict):
                        continue
                    task_name = str(preset_task.get("name") or "").strip()
                    definition = raw_task_definitions.get(task_name)
                    if definition is None or "ADB" not in _string_list(
                        definition.get("controller")
                    ):
                        continue
                    values = _initialize_option_values(
                        _string_list(definition.get("option")),
                        option_definitions,
                    )
                    preset_options = preset_task.get("option")
                    if isinstance(preset_options, dict):
                        for option_id, raw_value in preset_options.items():
                            option_definition = option_definitions.get(str(option_id))
                            if option_definition is None:
                                continue
                            converted = _convert_preset_option_value(
                                str(option_id),
                                raw_value,
                                option_definition,
                            )
                            if converted is not None:
                                values[str(option_id)] = converted
                    reachable = _reachable_option_ids(
                        _string_list(definition.get("option")),
                        option_definitions,
                    )
                    adb_task_configurations.append(
                        {
                            "name": task_name,
                            "enabled": preset_task.get("enabled") is not False,
                            "option_values": {
                                option_id: value
                                for option_id, value in values.items()
                                if option_id in reachable
                                and options.get(option_id, {}).get("adb_applicable") is True
                            },
                        }
                    )
            presets.append(
                {
                    "id": preset_name,
                    "name": preset_name,
                    "label": _localized_text(
                        row.get("label") or preset_name,
                        translations,
                        maximum=120,
                    ),
                    "description": _localized_text(
                        row.get("description"),
                        translations,
                        maximum=800,
                    ),
                    "task_names": task_names,
                    "task_count": len(task_names),
                    "adb_task_count": len(task_names) - len(unsupported),
                    "unsupported_task_names": unsupported,
                    "adb_compatible": bool(task_names) and not unsupported,
                    "adb_task_configurations": adb_task_configurations,
                    "order": index,
                }
            )

    adb_task_count = sum(task.get("adb_supported") is True for task in tasks)
    return {
        "schema_version": 2,
        "source": "installed_project_interface",
        "repository": MAAEND_REPOSITORY,
        "license": MAAEND_LICENSE,
        "version": str(interface.get("version") or "").strip(),
        "groups": groups,
        "tasks": tasks,
        "options": options,
        "presets": presets,
        "task_count": len(tasks),
        "adb_task_count": adb_task_count,
        "desktop_only_task_count": len(tasks) - adb_task_count,
    }


def _maaend_adb_device_name(device: str, adb: str) -> str:
    executable = shutil.which(adb) or adb
    try:
        adb_path = str(Path(executable).expanduser().resolve())
    except OSError:
        adb_path = str(executable)
    # MaaToolkit serializes Windows adb paths with forward slashes in the
    # device `name` field and MXU performs an exact string match.
    adb_path = adb_path.replace("\\", "/")
    return f"{device}-{adb_path}"


def _stable_task_id(task_name: str) -> str:
    return hashlib.sha256(
        f"{MAAEND_MANAGED_INSTANCE_ID}:{task_name}".encode("utf-8")
    ).hexdigest()[:12]


def configure_maaend_managed_profile(
    root: Path | str,
    *,
    device: str,
    adb: str,
    tasks: object = None,
    preset_name: str = "",
    resource_name: str = "",
) -> dict[str, object]:
    """Atomically create/update the one Mobile Profiler-owned MXU ADB instance.

    Other MXU instances and top-level preferences are retained verbatim. Only
    the dedicated instance ID/name is replaced, so this adapter never rewrites
    a user's Win32 profiles or imports MaaEnd's AGPL resources into this repo.
    """

    runtime_root = Path(root).expanduser().resolve()
    validate_maaend_runtime(runtime_root)
    interface = _load_interface_bundle(runtime_root)
    raw_tasks = interface.get("task")
    task_definitions = {
        str(item.get("name") or "").strip(): item
        for item in raw_tasks
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    } if isinstance(raw_tasks, list) else {}
    adb_task_definitions = {
        name: definition
        for name, definition in task_definitions.items()
        if "ADB" in _string_list(definition.get("controller"))
    }
    raw_options = interface.get("option")
    option_definitions = {
        str(option_id).strip(): definition
        for option_id, definition in raw_options.items()
        if str(option_id).strip() and isinstance(definition, dict)
    } if isinstance(raw_options, dict) else {}

    resources = interface.get("resource")
    resource_names = [
        str(item.get("name") or "").strip()
        for item in resources
        if isinstance(item, dict)
        and str(item.get("name") or "").strip()
        and _definition_compatible(
            item,
            controller="ADB",
            resource=str(item.get("name") or "").strip(),
        )
    ] if isinstance(resources, list) else []
    selected_resource = str(resource_name or "").strip() or next(
        iter(resource_names),
        "",
    )
    if not selected_resource or selected_resource not in resource_names:
        raise ValueError("MaaEnd 没有可用于 ADB 的资源配置")

    requested_rows: list[dict[str, object]] = []
    if isinstance(tasks, list):
        requested_rows = [item for item in tasks if isinstance(item, dict)]
        if len(requested_rows) != len(tasks):
            raise ValueError("MaaEnd tasks 只能包含对象")
    elif tasks is not None:
        raise ValueError("MaaEnd tasks 必须是数组")

    selected_preset = str(preset_name or "").strip()
    if not requested_rows and selected_preset:
        presets = interface.get("preset")
        preset = next(
            (
                item
                for item in presets
                if isinstance(item, dict)
                and str(item.get("name") or "").strip() == selected_preset
            ),
            None,
        ) if isinstance(presets, list) else None
        if preset is None:
            raise ValueError(f"MaaEnd 预设不存在: {selected_preset}")
        preset_tasks = preset.get("task")
        if isinstance(preset_tasks, list):
            for preset_task in preset_tasks:
                if not isinstance(preset_task, dict):
                    continue
                task_name = str(preset_task.get("name") or "").strip()
                definition = adb_task_definitions.get(task_name)
                if definition is None:
                    continue
                values = _initialize_option_values(
                    _string_list(definition.get("option")),
                    option_definitions,
                )
                preset_options = preset_task.get("option")
                if isinstance(preset_options, dict):
                    for option_id, raw_value in preset_options.items():
                        option_definition = option_definitions.get(str(option_id))
                        if option_definition is None:
                            continue
                        converted = _convert_preset_option_value(
                            str(option_id),
                            raw_value,
                            option_definition,
                        )
                        if converted is not None:
                            values[str(option_id)] = converted
                requested_rows.append(
                    {
                        "name": task_name,
                        "enabled": preset_task.get("enabled") is not False,
                        "option_values": values,
                    }
                )
    if not requested_rows:
        raise ValueError("请至少配置一项 MaaEnd ADB 任务")

    seen_tasks: set[str] = set()
    normalized_rows: list[dict[str, object]] = []
    for row in requested_rows:
        task_name = str(row.get("name") or row.get("taskName") or "").strip()
        if not task_name or task_name in seen_tasks:
            raise ValueError(f"MaaEnd 任务名称为空或重复: {task_name or '<empty>'}")
        seen_tasks.add(task_name)
        definition = adb_task_definitions.get(task_name)
        if definition is None:
            raise ValueError(f"MaaEnd 任务未声明支持 ADB: {task_name}")
        enabled = row.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError(f"MaaEnd 任务 {task_name} 的 enabled 必须是布尔值")
        option_values = row.get("option_values")
        if option_values is None and "optionValues" in row:
            option_values = row.get("optionValues")
        normalized_rows.append(
            {
                "name": task_name,
                "enabled": enabled,
                "option_values": _normalize_task_option_values(
                    definition,
                    option_values,
                    option_definitions,
                ),
            }
        )
    if not any(row["enabled"] is True for row in normalized_rows):
        raise ValueError("请至少启用一项 MaaEnd ADB 任务")

    config_path = runtime_root / "config" / MAAEND_CONFIG_FILENAME
    if config_path.is_file():
        config = load_maaend_profile_config(runtime_root)
    else:
        config = {
            "version": "1.0",
            "instances": [],
            "settings": {
                "theme": "system",
                "language": "system",
                "webServerEnabled": True,
                "webServerPort": _MXU_API_PORTS[0],
                "allowLanAccess": False,
            },
        }
    settings = config.get("settings")
    if settings is None:
        settings = {}
        config["settings"] = settings
    elif not isinstance(settings, dict):
        raise RuntimeError("MXU 配置 settings 必须是对象")
    # The adapter only talks to loopback. Preserve every explicit user value,
    # while making newly-created/legacy configs expose the API required for
    # deterministic task status.
    settings.setdefault("webServerEnabled", True)
    settings.setdefault("webServerPort", _MXU_API_PORTS[0])
    settings.setdefault("allowLanAccess", False)
    instances = config.get("instances")
    if not isinstance(instances, list):
        raise RuntimeError("MXU 配置 instances 必须是数组")
    existing_index = next(
        (
            index
            for index, item in enumerate(instances)
            if isinstance(item, dict)
            and (
                str(item.get("id") or "") == MAAEND_MANAGED_INSTANCE_ID
                or str(item.get("name") or "") == MAAEND_MANAGED_INSTANCE_NAME
            )
        ),
        None,
    )
    previous = (
        instances[existing_index]
        if existing_index is not None and isinstance(instances[existing_index], dict)
        else {}
    )
    previous_tasks = previous.get("tasks")
    previous_ids = {
        str(item.get("taskName") or "").strip(): str(item.get("id") or "").strip()
        for item in previous_tasks
        if isinstance(item, dict) and str(item.get("taskName") or "").strip()
    } if isinstance(previous_tasks, list) else {}
    managed_tasks = [
        {
            "id": previous_ids.get(str(row["name"])) or _stable_task_id(str(row["name"])),
            "taskName": row["name"],
            "enabled": row["enabled"],
            "enabledByController": {"ADB": row["enabled"]},
            "optionValues": row["option_values"],
        }
        for row in normalized_rows
    ]
    managed_instance = {
        **previous,
        "id": MAAEND_MANAGED_INSTANCE_ID,
        "name": MAAEND_MANAGED_INSTANCE_NAME,
        "controllerName": "ADB",
        "resourceName": selected_resource,
        "savedDevice": {
            "adbDeviceName": _maaend_adb_device_name(device, adb),
        },
        "tasks": managed_tasks,
        "preActions": [],
    }
    managed_instance.pop("preAction", None)
    if existing_index is None:
        instances.append(managed_instance)
    else:
        instances[existing_index] = managed_instance
    _atomic_write(
        config_path,
        (
            json.dumps(config, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8"),
    )
    return {
        "id": MAAEND_MANAGED_INSTANCE_ID,
        "name": MAAEND_MANAGED_INSTANCE_NAME,
        "controller": "ADB",
        "resource": selected_resource,
        "saved_device": managed_instance["savedDevice"]["adbDeviceName"],
        "task_names": [
            str(row["name"])
            for row in normalized_rows
            if row["enabled"] is True
        ],
        "task_count": sum(row["enabled"] is True for row in normalized_rows),
        "task_configurations": normalized_rows,
        "managed": True,
        "config_path": str(config_path),
    }


class _ProfileValidationError(RuntimeError):
    def __init__(self, screen_state: str, message: str) -> None:
        super().__init__(message)
        self.screen_state = screen_state


class _DeviceStateError(RuntimeError):
    def __init__(
        self,
        screen_state: str,
        message: str,
        state: dict[str, object],
    ) -> None:
        super().__init__(message)
        self.screen_state = screen_state
        self.state = state


def _adb_text_command(adb: str, device: str, *arguments: str) -> str:
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        result = subprocess.run(
            [adb, "-s", device, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"ADB 状态接口调用失败: {exc}") from exc
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        raise RuntimeError(stderr or stdout.strip() or "ADB 状态接口调用失败")
    return stdout


def _probe_maaend_device_state(
    adb: str,
    device: str,
    screenshot_path: Optional[Path] = None,
    *,
    allow_game_launch: bool = False,
) -> dict[str, object]:
    power = _adb_text_command(adb, device, "shell", "dumpsys", "power")
    activity = _adb_text_command(
        adb,
        device,
        "shell",
        "dumpsys",
        "activity",
        "activities",
    )
    awake_match = re.search(
        r"mWakefulness(?:Raw)?\s*=\s*(Awake|Asleep|Dozing|Dreaming)",
        power,
        flags=re.IGNORECASE,
    )
    wakefulness = awake_match.group(1).title() if awake_match else "Unknown"
    awake = wakefulness == "Awake"
    component_match = re.search(
        r"(?:topResumedActivity|mResumedActivity|ResumedActivity)[^\r\n]*?"
        r"\s([A-Za-z0-9_.]+)/(?:[A-Za-z0-9_.$]+)",
        activity,
    )
    foreground_package = component_match.group(1) if component_match else ""

    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        screenshot_result = subprocess.run(
            [adb, "-s", device, "exec-out", "screencap", "-p"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"ADB 截图接口调用失败: {exc}") from exc
    screenshot = screenshot_result.stdout
    screenshot_valid = (
        screenshot_result.returncode == 0
        and len(screenshot) >= 24
        and screenshot.startswith(b"\x89PNG\r\n\x1a\n")
    )
    width = 0
    height = 0
    if screenshot_valid:
        width, height = struct.unpack(">II", screenshot[16:24])
        if screenshot_path is not None:
            _atomic_write(screenshot_path, screenshot)
    state: dict[str, object] = {
        "wakefulness": wakefulness,
        "awake": awake,
        "foreground_package": foreground_package,
        "target_package": MAAEND_GAME_PACKAGE,
        "game_foreground": foreground_package == MAAEND_GAME_PACKAGE,
        "game_launch_allowed": allow_game_launch,
        "game_launch_required": (
            allow_game_launch and foreground_package != MAAEND_GAME_PACKAGE
        ),
        "screenshot_available": screenshot_valid,
        "screenshot": {
            "width": width,
            "height": height,
            "orientation": (
                "landscape" if width > height else "portrait" if height > width else "unknown"
            ),
        },
    }
    if not awake:
        raise _DeviceStateError(
            "device_asleep",
            f"Android 真机当前未亮屏（{wakefulness}）",
            state,
        )
    if not screenshot_valid:
        detail = screenshot_result.stderr.decode("utf-8", errors="replace").strip()
        raise _DeviceStateError(
            "vision_error",
            f"ADB 截图接口不可用: {detail or '未返回有效 PNG'}",
            state,
        )
    if foreground_package != MAAEND_GAME_PACKAGE:
        if allow_game_launch:
            return state
        raise _DeviceStateError(
            "wrong_app",
            (
                f"当前前台应用不是明日方舟：终末地（{foreground_package or '未识别'}）"
            ),
            state,
        )
    if width <= height:
        raise _DeviceStateError(
            "wrong_orientation",
            f"终末地画面不是横屏（{width} × {height}）",
            state,
        )
    return state


def _enabled_for_adb(task: dict[str, object]) -> bool:
    per_controller = task.get("enabledByController")
    if isinstance(per_controller, dict) and "ADB" in per_controller:
        return per_controller.get("ADB") is True
    return task.get("enabled") is True


def _profile_starts_game(profile: dict[str, object]) -> bool:
    names = profile.get("task_names")
    return bool(isinstance(names, list) and names and names[0] == "AndroidOpenGame")


def _profile_device_name(instance: dict[str, object], device: str) -> str:
    saved = instance.get("savedDevice")
    if not isinstance(saved, dict):
        raise _ProfileValidationError(
            "device_mismatch",
            "MaaEnd 实例未保存 ADB 设备，请先在 MaaEnd 中选择并保存设备",
        )
    normalized_device = device.casefold()
    direct_values = [
        str(saved.get(key) or "").strip()
        for key in ("address", "adbAddress", "device", "serial")
        if str(saved.get(key) or "").strip()
    ]
    if direct_values:
        if not any(value.casefold() == normalized_device for value in direct_values):
            raise _ProfileValidationError(
                "device_mismatch",
                f"MaaEnd 实例绑定的设备与当前真机 {device} 不一致",
            )
        return direct_values[0]

    saved_name = str(saved.get("adbDeviceName") or "").strip()
    # MaaToolkit names devices as "<serial>-<adb path/emulator name>".
    if not saved_name or not (
        saved_name.casefold() == normalized_device
        or saved_name.casefold().startswith(f"{normalized_device}-")
    ):
        raise _ProfileValidationError(
            "device_mismatch",
            f"MaaEnd 实例绑定的设备与当前真机 {device} 不一致",
        )
    return saved_name


def _validate_profile(
    interface: dict[str, object],
    config: dict[str, object],
    instance_name: str,
    device: str,
) -> dict[str, object]:
    instances = config.get("instances")
    if not isinstance(instances, list):
        raise _ProfileValidationError("profile_missing", "MXU 配置 instances 必须是数组")
    matches = [
        item
        for item in instances
        if isinstance(item, dict)
        and str(item.get("name") or "").strip() == instance_name
    ]
    if not matches:
        raise _ProfileValidationError(
            "profile_missing",
            f"MaaEnd 中不存在名为“{instance_name}”的实例",
        )
    if len(matches) > 1:
        raise _ProfileValidationError(
            "profile_missing",
            f"MaaEnd 中存在多个同名实例“{instance_name}”，请先改为唯一名称",
        )
    instance = matches[0]
    if str(instance.get("controllerName") or "").strip() != "ADB":
        raise _ProfileValidationError(
            "profile_incompatible",
            f"MaaEnd 实例“{instance_name}”没有使用 ADB 控制器",
        )

    resources = interface.get("resource")
    resource_names = {
        str(item.get("name") or "").strip()
        for item in resources
        if isinstance(item, dict)
    } if isinstance(resources, list) else set()
    resource_name = str(instance.get("resourceName") or "").strip()
    if not resource_name or resource_name not in resource_names:
        raise _ProfileValidationError(
            "profile_incompatible",
            f"MaaEnd 实例“{instance_name}”的资源配置无效",
        )

    pre_actions = instance.get("preActions")
    enabled_pre_actions = [
        item
        for item in pre_actions
        if isinstance(item, dict) and item.get("enabled") is not False
    ] if isinstance(pre_actions, list) else []
    legacy_pre_action = instance.get("preAction")
    if enabled_pre_actions or (
        isinstance(legacy_pre_action, dict)
        and legacy_pre_action.get("enabled") is True
    ):
        raise _ProfileValidationError(
            "unsafe_profile",
            "首版适配不允许运行含启用前置程序的 MaaEnd 实例",
        )

    tasks = instance.get("tasks")
    if not isinstance(tasks, list):
        raise _ProfileValidationError(
            "profile_incompatible",
            f"MaaEnd 实例“{instance_name}”的 tasks 必须是数组",
        )
    enabled_tasks = [
        task for task in tasks if isinstance(task, dict) and _enabled_for_adb(task)
    ]
    if not enabled_tasks:
        raise _ProfileValidationError(
            "profile_incompatible",
            f"MaaEnd 实例“{instance_name}”没有启用的 ADB 任务",
        )

    interface_tasks = interface.get("task")
    definitions = {
        str(item.get("name") or "").strip(): item
        for item in interface_tasks
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    } if isinstance(interface_tasks, list) else {}
    raw_options = interface.get("option")
    option_definitions = {
        str(option_id).strip(): definition
        for option_id, definition in raw_options.items()
        if str(option_id).strip() and isinstance(definition, dict)
    } if isinstance(raw_options, dict) else {}
    task_names: list[str] = []
    task_configurations: list[dict[str, object]] = []
    seen_task_names: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_name = str(task.get("taskName") or "").strip()
        definition = definitions.get(task_name)
        if definition is None or "ADB" not in _string_list(definition.get("controller")):
            continue
        if task_name in seen_task_names:
            raise _ProfileValidationError(
                "profile_incompatible",
                f"MaaEnd 实例包含重复任务: {task_name}",
            )
        seen_task_names.add(task_name)
        try:
            option_values = _normalize_task_option_values(
                definition,
                task.get("optionValues"),
                option_definitions,
            )
        except ValueError as exc:
            raise _ProfileValidationError(
                "profile_incompatible",
                str(exc),
            ) from exc
        task_configurations.append(
            {
                "id": str(task.get("id") or "").strip(),
                "name": task_name,
                "enabled": _enabled_for_adb(task),
                "option_values": option_values,
            }
        )
    for task in enabled_tasks:
        task_name = str(task.get("taskName") or "").strip()
        definition = definitions.get(task_name)
        if definition is None:
            raise _ProfileValidationError(
                "profile_incompatible",
                f"MaaEnd 实例包含未知任务: {task_name or '<empty>'}",
            )
        controllers = definition.get("controller")
        if not isinstance(controllers, list) or "ADB" not in controllers:
            raise _ProfileValidationError(
                "profile_incompatible",
                f"MaaEnd 任务 {task_name} 未声明支持 ADB",
            )
        task_names.append(task_name)

    saved_device = _profile_device_name(instance, device)
    return {
        "id": str(instance.get("id") or "").strip(),
        "name": instance_name,
        "controller": "ADB",
        "resource": resource_name,
        "saved_device": saved_device,
        "task_names": task_names,
        "task_count": len(task_names),
        "task_configurations": task_configurations,
        "managed": (
            str(instance.get("id") or "") == MAAEND_MANAGED_INSTANCE_ID
            or instance_name == MAAEND_MANAGED_INSTANCE_NAME
        ),
    }


class MaaEndRuntimeController:
    """Validate and run one configured MaaEnd MXU instance in a subprocess."""

    def __init__(
        self,
        output_root: Path,
        runtime_path: Optional[Path] = None,
        instance_name: str = "",
        *,
        adb: Optional[str] = None,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        http_json: Callable[..., object] = _mxu_http_json,
        http_bytes: Callable[..., bytes] = _mxu_http_bytes,
        api_host_preparer: Callable[[Path], Path] = _prepare_mxu_v220_api_host,
    ) -> None:
        runtime_root = output_root.resolve()
        self.output_root = (
            runtime_root / "open-source-automation" / "maaend"
        ).resolve()
        self._settings_path = self.output_root / "config.json"
        persisted = self._load_settings()
        environment_root = str(os.environ.get("MOBILE_PROFILER_MAAEND_ROOT") or "").strip()
        environment_instance = str(
            os.environ.get("MOBILE_PROFILER_MAAEND_INSTANCE") or ""
        ).strip()
        default_root = runtime_root / "open-source-runtimes" / "MaaEnd"
        selected_root = (
            runtime_path
            or (Path(environment_root) if environment_root else None)
            or (Path(str(persisted.get("runtime_path"))) if persisted.get("runtime_path") else None)
            or default_root
        )
        self.runtime_path = selected_root.expanduser().resolve()
        self.instance_name = (
            str(instance_name or "").strip()
            or environment_instance
            or str(persisted.get("instance_name") or "").strip()
            or MAAEND_MANAGED_INSTANCE_NAME
        )
        self.adb = str(adb or "").strip()
        self._popen_factory = popen_factory
        self._http_json = http_json
        self._http_bytes = http_bytes
        self._api_host_preparer = api_host_preparer
        self._lock = threading.RLock()
        self._status = "not_installed"
        self._running = False
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._process_owned = False
        self._log_handle = None
        self._stop_event = threading.Event()
        self._device = ""
        self._last_error = ""
        self._last_preflight: Optional[dict[str, object]] = None
        self._last_preflight_context: Optional[dict[str, str]] = None
        self._last_profile: Optional[dict[str, object]] = None
        self._last_run_dir = ""
        self._last_exit_code: Optional[int] = None
        self._started_at: Optional[float] = None
        self._completed_at: Optional[float] = None
        self._runtime_metadata: dict[str, object] = {}
        self._runtime_metadata_path = ""
        self._disk_bytes = 0
        self._last_install_check_at = 0.0
        self._last_install_check_path = ""
        self._last_install_available = False
        self._last_install_upstream: dict[str, object] = {}
        self._last_install_error = ""
        self._game_catalog_cache: dict[str, object] = {}
        self._game_catalog_cache_key = ""
        self._profile_catalog_cache: list[dict[str, object]] = []
        self._profile_catalog_cache_key = ""
        self._profile_catalog_error = ""
        self._api_base_url = ""
        self._api_instance_id = ""
        self._api_phase = "idle"
        self._api_maafw_version = ""
        self._api_instance_state: dict[str, object] = {}
        self._api_tasks: list[dict[str, object]] = []
        self._api_task_ids: list[int] = []
        self._api_resource_paths: list[str] = []
        self._api_updated_at: Optional[float] = None
        self._logs: deque[dict[str, object]] = deque(maxlen=30)
        self._refresh_install_status()
        self._log(self._status, "MaaEnd 外部运行时适配器已加载")

    def _load_settings(self) -> dict[str, object]:
        if not self._settings_path.is_file():
            return {}
        try:
            value = _load_jsonc(self._settings_path)
        except RuntimeError:
            return {}
        return value

    def _save_settings(self) -> None:
        payload = {
            "schema_version": 1,
            "runtime_path": str(self.runtime_path),
            "instance_name": self.instance_name,
        }
        _atomic_write(
            self._settings_path,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def _log(self, status: str, message: str) -> None:
        with self._lock:
            self._logs.append(
                {"time": time.time(), "status": status, "message": message}
            )

    def _refresh_install_status(self) -> tuple[bool, dict[str, object], str]:
        root_key = str(self.runtime_path)
        now = time.monotonic()
        if (
            self._last_install_check_path == root_key
            and now - self._last_install_check_at < 3.0
        ):
            return (
                self._last_install_available,
                dict(self._last_install_upstream),
                self._last_install_error,
            )
        try:
            metadata = validate_maaend_runtime(self.runtime_path)
        except Exception as exc:
            error = str(exc)
            upstream = {
                "path": str(self.runtime_path),
                "repository": MAAEND_REPOSITORY,
                "license": MAAEND_LICENSE,
                "version": "",
                "error": error,
            }
            self._last_install_check_at = now
            self._last_install_check_path = root_key
            self._last_install_available = False
            self._last_install_upstream = upstream
            self._last_install_error = error
            with self._lock:
                if not self._running and self._status in {"not_installed", "installed"}:
                    self._status = "not_installed"
                if not self._last_error or self._status == "not_installed":
                    self._last_error = error
            return False, upstream, error
        if self._runtime_metadata_path != root_key:
            self._disk_bytes = _directory_size(self.runtime_path)
        self._runtime_metadata = metadata
        self._runtime_metadata_path = root_key
        self._last_install_check_at = now
        self._last_install_check_path = root_key
        self._last_install_available = True
        self._last_install_upstream = metadata
        self._last_install_error = ""
        with self._lock:
            if not self._running and self._status in {"not_installed", "installed"}:
                self._status = "installed"
                self._last_error = ""
        return True, metadata, ""

    def _configuration_from_payload(
        self,
        payload: dict[str, object],
    ) -> tuple[Path, str]:
        raw_path = (
            payload.get("runtime_path")
            if "runtime_path" in payload
            else str(self.runtime_path)
        )
        path_text = str(raw_path or "").strip()
        if not path_text:
            raise ValueError("MaaEnd 发布目录不能为空")
        root = Path(path_text).expanduser().resolve()
        raw_instance = (
            payload.get("instance_name")
            if "instance_name" in payload
            else self.instance_name
        )
        selected_instance = str(raw_instance or "").strip()
        if not selected_instance:
            raise ValueError("MaaEnd 实例名不能为空")
        return root, selected_instance

    def configure(self, payload: dict[str, object]) -> dict[str, object]:
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("保存 MaaEnd ADB 任务需要 Android USB 设备")
        raw_path = payload.get("runtime_path", str(self.runtime_path))
        path_text = str(raw_path or "").strip()
        if not path_text:
            raise ValueError("MaaEnd 发布目录不能为空")
        root = Path(path_text).expanduser().resolve()
        with self._lock:
            if self._running:
                raise RuntimeError("MaaEnd 运行中，不能改写任务配置")
            self.runtime_path = root
            self.instance_name = MAAEND_MANAGED_INSTANCE_NAME
            self._status = "configuring"
            self._last_error = ""
            self._last_preflight = None
            self._last_preflight_context = None
            self._last_profile = None
            self._api_base_url = ""
            self._api_instance_id = ""
            self._api_phase = "idle"
            self._api_maafw_version = ""
            self._api_instance_state = {}
            self._api_tasks = []
            self._api_task_ids = []
            self._api_resource_paths = []
            self._api_updated_at = None
            self._last_install_check_path = ""
            self._last_install_check_at = 0.0
            self._game_catalog_cache_key = ""
            self._profile_catalog_cache_key = ""
        self._save_settings()
        self._log("configuring", f"正在保存 MaaEnd ADB 任务到专用实例（设备 {device}）")
        try:
            profile = configure_maaend_managed_profile(
                root,
                device=device,
                adb=self.adb or "adb",
                tasks=payload.get("tasks"),
                preset_name=str(payload.get("preset_name") or ""),
                resource_name=str(payload.get("resource_name") or ""),
            )
        except Exception as exc:
            with self._lock:
                self._status = "error"
                self._last_error = str(exc)
            self._log("error", str(exc))
            raise
        with self._lock:
            self._device = device
            self._status = "installed"
            self._last_error = ""
            self._profile_catalog_cache_key = ""
        self._log(
            "configured",
            (
                f"已保存 {len(profile['task_configurations'])} 项 MaaEnd ADB 任务，"
                f"其中 {profile['task_count']} 项启用"
            ),
        )
        return self.snapshot()

    def _game_catalog_snapshot(self, available: bool) -> dict[str, object]:
        if not available:
            return {
                "schema_version": 2,
                "source": "runtime_required",
                "repository": MAAEND_REPOSITORY,
                "license": MAAEND_LICENSE,
                "version": "",
                "groups": [],
                "tasks": [],
                "options": {},
                "presets": [],
                "task_count": 0,
                "adb_task_count": 0,
                "desktop_only_task_count": 0,
            }
        interface_path = self.runtime_path / "interface.json"
        try:
            stat = interface_path.stat()
            cache_key = (
                f"{self.runtime_path}|{stat.st_mtime_ns}|{stat.st_size}"
            )
            if cache_key != self._game_catalog_cache_key:
                self._game_catalog_cache = load_maaend_game_catalog(
                    self.runtime_path
                )
                self._game_catalog_cache_key = cache_key
            return self._game_catalog_cache
        except Exception as exc:
            return {
                "schema_version": 2,
                "source": "catalog_error",
                "repository": MAAEND_REPOSITORY,
                "license": MAAEND_LICENSE,
                "version": "",
                "groups": [],
                "tasks": [],
                "options": {},
                "presets": [],
                "task_count": 0,
                "adb_task_count": 0,
                "desktop_only_task_count": 0,
                "error": str(exc),
            }

    def _configured_profiles_snapshot(
        self,
        available: bool,
    ) -> tuple[list[dict[str, object]], str]:
        if not available:
            return [], ""
        config_path = self.runtime_path / "config" / MAAEND_CONFIG_FILENAME
        try:
            stat = config_path.stat()
            cache_key = f"{config_path}|{stat.st_mtime_ns}|{stat.st_size}"
            if cache_key == self._profile_catalog_cache_key:
                return self._profile_catalog_cache, self._profile_catalog_error
            config = load_maaend_profile_config(self.runtime_path)
            raw_instances = config.get("instances")
            profiles: list[dict[str, object]] = []
            if isinstance(raw_instances, list):
                for row in raw_instances:
                    if not isinstance(row, dict):
                        continue
                    raw_tasks = row.get("tasks")
                    task_names = [
                        str(task.get("taskName") or "").strip()
                        for task in raw_tasks
                        if isinstance(task, dict)
                        and _enabled_for_adb(task)
                        and str(task.get("taskName") or "").strip()
                    ] if isinstance(raw_tasks, list) else []
                    task_configurations = [
                        {
                            "name": str(task.get("taskName") or "").strip(),
                            "enabled": _enabled_for_adb(task),
                            "option_values": (
                                task.get("optionValues")
                                if isinstance(task.get("optionValues"), dict)
                                else {}
                            ),
                        }
                        for task in raw_tasks
                        if isinstance(task, dict)
                        and str(task.get("taskName") or "").strip()
                    ] if isinstance(raw_tasks, list) else []
                    saved = row.get("savedDevice")
                    saved_device = ""
                    if isinstance(saved, dict):
                        saved_device = next(
                            (
                                str(saved.get(key) or "").strip()
                                for key in (
                                    "adbDeviceName",
                                    "address",
                                    "adbAddress",
                                    "device",
                                    "serial",
                                )
                                if str(saved.get(key) or "").strip()
                            ),
                            "",
                        )
                    pre_actions = row.get("preActions")
                    enabled_pre_actions = any(
                        isinstance(item, dict)
                        and item.get("enabled") is not False
                        for item in pre_actions
                    ) if isinstance(pre_actions, list) else False
                    legacy_pre_action = row.get("preAction")
                    enabled_pre_actions = enabled_pre_actions or (
                        isinstance(legacy_pre_action, dict)
                        and legacy_pre_action.get("enabled") is True
                    )
                    profiles.append(
                        {
                            "id": str(row.get("id") or "").strip(),
                            "name": str(row.get("name") or "").strip(),
                            "controller": str(
                                row.get("controllerName") or ""
                            ).strip(),
                            "resource": str(
                                row.get("resourceName") or ""
                            ).strip(),
                            "saved_device": saved_device,
                            "task_names": task_names,
                            "task_count": len(task_names),
                            "task_configurations": task_configurations,
                            "managed": (
                                str(row.get("id") or "") == MAAEND_MANAGED_INSTANCE_ID
                                or str(row.get("name") or "")
                                == MAAEND_MANAGED_INSTANCE_NAME
                            ),
                            "has_enabled_pre_actions": enabled_pre_actions,
                        }
                    )
            self._profile_catalog_cache = profiles
            self._profile_catalog_cache_key = cache_key
            self._profile_catalog_error = ""
            return profiles, ""
        except Exception as exc:
            error = str(exc)
            self._profile_catalog_cache = []
            self._profile_catalog_cache_key = ""
            self._profile_catalog_error = error
            return [], error

    def _preflight_failure(
        self,
        device: str,
        screen_state: str,
        error: Exception,
        device_state: Optional[dict[str, object]] = None,
    ) -> None:
        message = str(error)
        summary: dict[str, object] = {
            "adapter": MAAEND_ADAPTER_ID,
            "device": {"serial": device},
            "screen": {
                "screen_state": screen_state,
                "game_ready": False,
                **(device_state or {}),
            },
            "profile": {
                "name": self.instance_name,
                "task_names": [],
            },
        }
        with self._lock:
            self._status = "error"
            self._last_error = message
            self._last_preflight = summary
            self._last_preflight_context = None
            self._last_profile = None
        self._log("error", message)

    def preflight(self, payload: dict[str, object]) -> dict[str, object]:
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("MaaEnd 预检需要 Android 设备")
        with self._lock:
            if self._running:
                raise RuntimeError("MaaEnd 已在运行")
        try:
            root, instance_name = self._configuration_from_payload(payload)
        except Exception as exc:
            self._preflight_failure(device, "profile_missing", exc)
            raise
        with self._lock:
            self.runtime_path = root
            self.instance_name = instance_name
            self._last_install_check_path = ""
            self._last_install_check_at = 0.0
            self._game_catalog_cache_key = ""
            self._profile_catalog_cache_key = ""
            self._status = "preflighting"
            self._last_error = ""
            self._device = device
            self._api_base_url = ""
            self._api_instance_id = ""
            self._api_phase = "idle"
            self._api_maafw_version = ""
            self._api_instance_state = {}
            self._api_tasks = []
            self._api_task_ids = []
            self._api_resource_paths = []
            self._api_updated_at = None
        self._save_settings()
        self._log(
            "preflighting",
            f"开始检查 MaaEnd 实例“{instance_name}”与真机 {device}",
        )
        try:
            validate_maaend_runtime(root)
        except Exception as exc:
            self._preflight_failure(device, "runtime_missing", exc)
            raise RuntimeError(str(exc)) from exc
        try:
            interface = _load_interface_bundle(root)
            config = load_maaend_profile_config(root)
            profile = _validate_profile(interface, config, instance_name, device)
        except _ProfileValidationError as exc:
            self._preflight_failure(device, exc.screen_state, exc)
            raise RuntimeError(str(exc)) from exc
        except Exception as exc:
            self._preflight_failure(device, "profile_missing", exc)
            raise RuntimeError(str(exc)) from exc

        device_state: dict[str, object] = {}
        if self.adb:
            try:
                device_state = _probe_maaend_device_state(
                    self.adb,
                    device,
                    self.output_root / "preflight-screen.png",
                    allow_game_launch=_profile_starts_game(profile),
                )
            except _DeviceStateError as exc:
                self._preflight_failure(
                    device,
                    exc.screen_state,
                    exc,
                    exc.state,
                )
                raise RuntimeError(str(exc)) from exc
            except Exception as exc:
                self._preflight_failure(device, "vision_error", exc)
                raise RuntimeError(str(exc)) from exc

        summary: dict[str, object] = {
            "adapter": MAAEND_ADAPTER_ID,
            "device": {"serial": device},
            "screen": {
                "screen_state": (
                    "maaend_launch_ready"
                    if device_state.get("game_launch_required") is True
                    else "maaend_profile_ready"
                ),
                "game_ready": True,
                "state_source": "adb_interfaces" if self.adb else "config_only",
                **device_state,
            },
            "profile": profile,
        }
        with self._lock:
            self._last_preflight = summary
            self._last_preflight_context = {
                "runtime_path": str(root),
                "instance_name": instance_name,
                "device": device,
            }
            self._last_profile = profile
            self._status = "ready"
            self._last_error = ""
        _atomic_write(
            self.output_root / "preflight.json",
            json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        self._log(
            "ready",
            f"MaaEnd 实例“{instance_name}”已通过 ADB 配置预检（{len(profile['task_names'])} 项任务）",
        )
        return self.snapshot()

    def _command(self, root: Path) -> list[str]:
        # Task submission is performed through MXU's loopback HTTP API.  No
        # autostart flag is used because process exit is not a task result. The
        # verified sibling host skips only MXU 2.3.0's unconditional UAC branch;
        # all MaaEnd v2.20 resources and API code remain the official release.
        return [str(self._api_host_preparer(root))]

    def _set_api_phase(self, phase: str, message: str = "") -> None:
        with self._lock:
            self._api_phase = phase
            self._api_updated_at = time.time()
        if message:
            self._log(phase, message)

    def _api_call(
        self,
        method: str,
        path: str,
        payload: object = None,
        *,
        timeout: float = 15.0,
    ) -> object:
        with self._lock:
            base_url = self._api_base_url
        if not base_url:
            raise RuntimeError("MaaEnd MXU API 尚未就绪")
        return self._http_json(
            base_url,
            method,
            path,
            payload,
            timeout=timeout,
        )

    def _verify_api_controller_screenshot(self) -> dict[str, int]:
        with self._lock:
            base_url = self._api_base_url
        if not base_url:
            raise RuntimeError("MaaEnd MXU API 尚未就绪")
        screenshot = self._http_bytes(
            base_url,
            "GET",
            self._instance_api_path("/screenshot"),
            timeout=_MXU_SCREENSHOT_TIMEOUT_SECONDS,
        )
        if len(screenshot) < 24 or not screenshot.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("MXU ADB 控制器未返回有效 PNG 截图")
        width, height = struct.unpack(">II", screenshot[16:24])
        if width <= 0 or height <= 0:
            raise RuntimeError("MXU ADB 控制器返回的截图尺寸无效")
        _atomic_write(self.output_root / "mxu-connection-screen.png", screenshot)
        return {"width": width, "height": height}

    def _instance_api_path(self, suffix: str = "") -> str:
        with self._lock:
            instance_id = self._api_instance_id
        if not instance_id:
            raise RuntimeError("MaaEnd MXU 实例 ID 为空")
        return f"/maa/instances/{quote(instance_id, safe='')}{suffix}"

    def _discover_api(
        self,
        root: Path,
        process: subprocess.Popen[bytes],
    ) -> tuple[str, str, bool]:
        deadline = time.monotonic() + _MXU_API_STARTUP_TIMEOUT_SECONDS
        last_error = ""
        while time.monotonic() < deadline:
            for port in _MXU_API_PORTS:
                base_url = f"http://127.0.0.1:{port}/api"
                try:
                    response = self._http_json(
                        base_url,
                        "GET",
                        "/interface",
                        timeout=0.8,
                    )
                except Exception as exc:
                    last_error = str(exc)
                    continue
                if not isinstance(response, dict):
                    continue
                api_interface = response.get("interface")
                base_path = str(response.get("basePath") or "").strip()
                if not isinstance(api_interface, dict) or not base_path:
                    continue
                try:
                    same_root = os.path.normcase(str(Path(base_path).resolve())) == os.path.normcase(
                        str(root)
                    )
                except OSError:
                    same_root = False
                if not same_root:
                    continue
                api_version = str(api_interface.get("version") or "").strip()
                if api_version != MAAEND_STANDARD_VERSION:
                    raise RuntimeError(
                        f"MXU API 加载的 MaaEnd 版本不是 {MAAEND_STANDARD_VERSION}: "
                        f"{api_version or '未知版本'}"
                    )
                initialized = self._http_json(
                    base_url,
                    "GET",
                    "/maa/initialized",
                    timeout=3.0,
                )
                if not isinstance(initialized, dict) or initialized.get("initialized") is not True:
                    last_error = "MaaFramework 尚未初始化"
                    continue
                maafw_version = str(initialized.get("version") or "").strip()
                return base_url, maafw_version, process.poll() is None
            if process.poll() is not None:
                raise RuntimeError(
                    f"MaaEnd 在 MXU API 就绪前退出（退出码 {process.poll()}）"
                )
            if self._stop_event.wait(0.2):
                raise RuntimeError("MaaEnd 启动已取消")
        raise RuntimeError(
            "等待 MaaEnd MXU API 超时"
            + (f": {last_error}" if last_error else "")
        )

    def _read_instance_state(self) -> dict[str, object]:
        response = self._api_call("GET", "/maa/state", timeout=5.0)
        instances = response.get("instances") if isinstance(response, dict) else None
        with self._lock:
            instance_id = self._api_instance_id
        state = instances.get(instance_id) if isinstance(instances, dict) else None
        if not isinstance(state, dict):
            raise RuntimeError(f"MXU API 状态中不存在实例 {instance_id}")
        self._update_instance_state(state)
        return state

    def _update_instance_state(self, state: dict[str, object]) -> None:
        run_state = state.get("task_run_state")
        statuses = run_state.get("statuses") if isinstance(run_state, dict) else None
        mappings = run_state.get("mappings") if isinstance(run_state, dict) else None
        status_rows = statuses if isinstance(statuses, dict) else {}
        mapping_rows = mappings if isinstance(mappings, dict) else {}
        selected_to_maa: dict[str, int] = {}
        for raw_task_id, raw_selected_id in mapping_rows.items():
            try:
                maa_task_id = int(raw_task_id)
            except (TypeError, ValueError):
                continue
            selected_to_maa[str(raw_selected_id)] = maa_task_id
        with self._lock:
            self._api_instance_state = json.loads(json.dumps(state))
            for task in self._api_tasks:
                selected_id = str(task.get("id") or "")
                task["status"] = str(status_rows.get(selected_id) or "pending")
                if selected_id in selected_to_maa:
                    task["maa_task_id"] = selected_to_maa[selected_id]
            self._api_updated_at = time.time()

    def _wait_for_instance_state(
        self,
        process: subprocess.Popen[bytes],
        predicate: Callable[[dict[str, object]], bool],
        *,
        timeout: float,
        description: str,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                raise RuntimeError("MaaEnd 启动已取消")
            if self._process_owned and process.poll() is not None:
                raise RuntimeError(
                    f"MaaEnd 在{description}时退出（退出码 {process.poll()}）"
                )
            try:
                state = self._read_instance_state()
            except Exception as exc:
                last_error = str(exc)
            else:
                if predicate(state):
                    return state
            self._stop_event.wait(0.25)
        raise RuntimeError(
            f"等待 MaaEnd {description}超时"
            + (f": {last_error}" if last_error else "")
        )

    def _submit_api_tasks(
        self,
        process: subprocess.Popen[bytes],
        root: Path,
        interface: dict[str, object],
        config: dict[str, object],
        profile: dict[str, object],
        task_requests: list[dict[str, object]],
        task_metadata: list[dict[str, object]],
        resource_paths: list[str],
    ) -> None:
        instance_path = self._instance_api_path()
        self._api_call("PUT", instance_path, timeout=10.0)

        self._set_api_phase("discovering_device", "正在通过 MXU API 扫描 ADB 设备")
        devices = self._api_call("GET", "/maa/devices", timeout=35.0)
        if not isinstance(devices, list):
            raise RuntimeError("MXU API 未返回 ADB 设备数组")
        with self._lock:
            device = self._device
        saved_device = str(profile.get("saved_device") or "").strip()
        matched = next(
            (
                row
                for row in devices
                if isinstance(row, dict)
                and str(row.get("address") or "") == device
                and (
                    saved_device == device
                    or str(row.get("name") or "") == saved_device
                )
            ),
            None,
        )
        if not isinstance(matched, dict):
            discovered = ", ".join(
                str(row.get("name") or row.get("address") or "")
                for row in devices
                if isinstance(row, dict)
            )
            raise RuntimeError(
                f"MXU API 未精确匹配 USB 设备 {saved_device or device}"
                + (f"；扫描结果: {discovered}" if discovered else "")
            )

        controller = _named_interface_definition(interface, "controller", "ADB")
        screencap_methods = str(matched.get("screencap_methods") or "")
        input_methods = str(matched.get("input_methods") or "")
        try:
            screencap_mask = int(screencap_methods)
            input_mask = int(input_methods)
        except ValueError as exc:
            raise RuntimeError("MXU API 返回了无效的 ADB 控制器方法位掩码") from exc
        used_generic_fallback = False
        if not screencap_mask & _ADB_GENERIC_SCREENCAP_METHODS:
            screencap_methods = str(_ADB_GENERIC_SCREENCAP_METHODS)
            used_generic_fallback = True
        if not input_mask & _ADB_GENERIC_INPUT_METHODS:
            input_methods = str(_ADB_GENERIC_INPUT_METHODS)
            used_generic_fallback = True
        connect_payload: dict[str, object] = {
            "type": "Adb",
            "adb_path": str(matched.get("adb_path") or ""),
            "address": str(matched.get("address") or ""),
            "screencap_methods": screencap_methods,
            "input_methods": input_methods,
            "config": str(matched.get("config") or "{}"),
        }
        display_short_side = controller.get("display_short_side")
        if isinstance(display_short_side, int) and not isinstance(display_short_side, bool):
            connect_payload["display_short_side"] = display_short_side
        if used_generic_fallback:
            self._log(
                "connecting",
                "设备发现仅返回 EmulatorExtras/Androws，改用兼容真机显示 ID 的通用 ADB 截图与输入方法",
            )
        self._set_api_phase("connecting", f"正在连接 USB ADB 设备 {device}")
        self._api_call(
            "POST",
            f"{instance_path}/connect",
            connect_payload,
            timeout=35.0,
        )
        state = self._wait_for_instance_state(
            process,
            lambda row: row.get("connected") is True,
            timeout=_MXU_CONNECT_TIMEOUT_SECONDS,
            description="连接 ADB 控制器",
        )
        self._set_api_phase("verifying_controller", "正在通过 MXU API 验证 ADB 控制器截图")
        screenshot = self._verify_api_controller_screenshot()
        self._log(
            "verifying_controller",
            f"MXU ADB 控制器截图验证成功（{screenshot['width']}x{screenshot['height']}）",
        )

        self._set_api_phase("loading_resource", "正在加载 MaaEnd v2.20 ADB 资源")
        if state.get("resource_loaded") is not True:
            self._api_call(
                "POST",
                f"{instance_path}/resource/load",
                {"paths": resource_paths},
                timeout=20.0,
            )
            self._wait_for_instance_state(
                process,
                lambda row: row.get("resource_loaded") is True,
                timeout=_MXU_RESOURCE_TIMEOUT_SECONDS,
                description="加载 ADB 资源",
            )

        settings = config.get("settings")
        tcp_compat_mode = (
            settings.get("tcpCompatMode") is True
            if isinstance(settings, dict)
            else False
        )
        agent_configs = _mxu_agent_configs(interface)
        pi_envs = _mxu_pi_envs(
            interface,
            controller_name="ADB",
            resource_name=str(profile.get("resource") or ""),
            maafw_version=self._api_maafw_version,
        ) if agent_configs else {}
        payload: dict[str, object] = {
            "tasks": task_requests,
            "agent_configs": agent_configs,
            "cwd": str(root).replace("\\", "/"),
            "tcp_compat_mode": tcp_compat_mode,
            "pi_envs": pi_envs,
            "reset_state": True,
        }
        self._set_api_phase(
            "submitting_tasks",
            f"正在通过 MXU API 提交 {len(task_requests)} 项 ADB 任务",
        )
        response = self._api_call(
            "POST",
            f"{instance_path}/tasks/start",
            payload,
            timeout=90.0,
        )
        raw_task_ids = response.get("taskIds") if isinstance(response, dict) else None
        if not isinstance(raw_task_ids, list):
            raise RuntimeError("MXU API 启动响应缺少 taskIds")
        try:
            task_ids = [int(item) for item in raw_task_ids]
        except (TypeError, ValueError) as exc:
            raise RuntimeError("MXU API 返回了无效 taskIds") from exc
        if len(task_ids) != len(task_requests):
            raise RuntimeError(
                f"MXU 只接受了 {len(task_ids)}/{len(task_requests)} 项任务，已拒绝判定成功"
            )
        for task, task_id in zip(task_metadata, task_ids):
            task["maa_task_id"] = task_id
        with self._lock:
            self._api_task_ids = task_ids
            self._api_tasks = task_metadata
            self._api_updated_at = time.time()
        self._set_api_phase("running", "MXU API 已接受全部任务，开始轮询逐任务状态")

    @staticmethod
    def _close_log_handle(log_handle) -> None:
        try:
            log_handle.flush()
            log_handle.close()
        except (AttributeError, OSError, ValueError):
            pass

    def _terminate_process(
        self,
        process: subprocess.Popen[bytes],
        *,
        owned: bool,
    ) -> int:
        polled = process.poll()
        if polled is not None:
            return int(polled)
        if not owned:
            return 0
        try:
            process.terminate()
            return int(process.wait(timeout=8))
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                return int(process.wait(timeout=5))
            except (OSError, subprocess.TimeoutExpired):
                return -9

    def _finalize_process(
        self,
        process: subprocess.Popen[bytes],
        exit_code: int,
        log_handle,
        *,
        status: str,
        error: str = "",
    ) -> None:
        self._close_log_handle(log_handle)
        should_log = False
        with self._lock:
            if self._process is process:
                self._process = None
                self._process_owned = False
                self._log_handle = None
                self._running = False
                self._last_exit_code = int(exit_code)
                self._completed_at = time.time()
                self._status = status
                self._last_error = error
                self._api_phase = status
                self._api_updated_at = self._completed_at
                should_log = True
        if should_log:
            default_message = {
                "completed": "MaaEnd 全部 ADB 任务已由 MXU API 确认成功",
                "stopped": "MaaEnd ADB 任务已停止",
                "error": "MaaEnd ADB 任务执行失败",
            }.get(status, "MaaEnd 外部运行时已结束")
            self._log(status, error or default_message)

    def _stop_api_instance(self) -> None:
        with self._lock:
            base_url = self._api_base_url
            instance_id = self._api_instance_id
        if not base_url or not instance_id:
            return
        instance_path = f"/maa/instances/{quote(instance_id, safe='')}"
        for suffix in ("/tasks/stop", "/agent/stop"):
            try:
                self._http_json(
                    base_url,
                    "POST",
                    f"{instance_path}{suffix}",
                    timeout=8.0,
                )
            except Exception as exc:
                self._log("stopping", f"MXU API {suffix} 未完成: {exc}")

    def _watch_api_run(
        self,
        process: subprocess.Popen[bytes],
        log_handle,
    ) -> None:
        idle_since: Optional[float] = None
        consecutive_api_errors = 0
        terminal_status = ""
        terminal_error = ""
        while not self._stop_event.wait(0.5):
            try:
                state = self._read_instance_state()
                consecutive_api_errors = 0
            except Exception as exc:
                consecutive_api_errors += 1
                if self._process_owned and process.poll() is not None:
                    terminal_status = "error"
                    terminal_error = (
                        f"MaaEnd 进程在任务取得终态前退出（退出码 {process.poll()}）"
                    )
                    break
                if consecutive_api_errors >= 10:
                    terminal_status = "error"
                    terminal_error = f"连续无法读取 MXU API 任务状态: {exc}"
                    break
                continue

            run_state = state.get("task_run_state")
            statuses = run_state.get("statuses") if isinstance(run_state, dict) else None
            overall = str(
                run_state.get("overall_status") or ""
            ) if isinstance(run_state, dict) else ""
            status_map = statuses if isinstance(statuses, dict) else {}
            with self._lock:
                expected_ids = [str(task.get("id") or "") for task in self._api_tasks]
                failed_names = [
                    str(task.get("name") or task.get("id") or "未知任务")
                    for task in self._api_tasks
                    if str(status_map.get(str(task.get("id") or "")) or "") == "failed"
                ]
            expected_statuses = [str(status_map.get(item) or "") for item in expected_ids]
            if expected_ids and all(status == "succeeded" for status in expected_statuses):
                terminal_status = "completed"
                break
            if failed_names or overall == "Failed":
                terminal_status = "error"
                terminal_error = "MaaEnd 任务失败: " + (
                    ", ".join(failed_names) if failed_names else "MXU 整体状态为 Failed"
                )
                break
            if overall == "Succeeded":
                missing = [
                    expected_ids[index]
                    for index, status in enumerate(expected_statuses)
                    if status != "succeeded"
                ]
                terminal_status = "error"
                terminal_error = (
                    "MXU 整体状态为 Succeeded，但并非全部任务状态为 succeeded: "
                    + ", ".join(missing)
                )
                break

            if state.get("is_running") is True:
                idle_since = None
            elif idle_since is None:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= 15.0:
                terminal_status = "error"
                terminal_error = "MXU Tasker 已停止，但逐任务状态没有形成完整终态"
                break

        if self._stop_event.is_set():
            return
        self._stop_api_instance()
        with self._lock:
            owned = self._process_owned
        exit_code = self._terminate_process(process, owned=owned)
        self._finalize_process(
            process,
            exit_code,
            log_handle,
            status=terminal_status or "error",
            error=terminal_error,
        )

    def start(self, payload: dict[str, object]) -> dict[str, object]:
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("MaaEnd 启动需要 Android 设备")
        root, instance_name = self._configuration_from_payload(payload)
        with self._lock:
            if self._running:
                raise RuntimeError("MaaEnd 已在运行")
            expected = dict(self._last_preflight_context or {})
            expected_profile = dict(self._last_profile or {})
        requested = {
            "runtime_path": str(root),
            "instance_name": instance_name,
            "device": device,
        }
        if expected != requested:
            raise RuntimeError("请先为当前 MaaEnd 目录、实例和设备运行成功预检")

        # Revalidate immediately before launch so edits to the MXU profile cannot
        # introduce desktop-only tasks or arbitrary pre-actions after preflight.
        try:
            validate_maaend_runtime(root)
            interface = _load_interface_bundle(root)
            config = load_maaend_profile_config(root)
            profile = _validate_profile(interface, config, instance_name, device)
        except Exception as exc:
            with self._lock:
                self._last_preflight_context = None
                self._last_profile = None
                self._last_preflight = None
                self._status = "error"
                self._last_error = f"MaaEnd 启动前复检失败: {exc}"
            self._log("error", self._last_error)
            raise RuntimeError(self._last_error) from exc
        if profile != expected_profile:
            with self._lock:
                self._last_preflight_context = None
                self._last_profile = None
                self._last_preflight = None
                self._status = "error"
                self._last_error = "MaaEnd 实例在预检后发生变化，请重新预检"
            self._log("error", self._last_error)
            raise RuntimeError(self._last_error)

        if self.adb:
            try:
                _probe_maaend_device_state(
                    self.adb,
                    device,
                    self.output_root / "launch-screen.png",
                    allow_game_launch=_profile_starts_game(profile),
                )
            except Exception as exc:
                with self._lock:
                    self._last_preflight_context = None
                    self._last_profile = None
                    self._status = "error"
                    self._last_error = f"MaaEnd 启动前设备状态复检失败: {exc}"
                self._log("error", self._last_error)
                raise RuntimeError(self._last_error) from exc

        resource_paths = _runtime_resource_paths(
            root,
            interface,
            controller_name="ADB",
            resource_name=str(profile.get("resource") or ""),
        )
        task_requests, task_metadata = _mxu_task_requests(interface, config, profile)
        instance_id = str(profile.get("id") or "").strip()
        if not instance_id:
            raise RuntimeError("MaaEnd 实例缺少稳定 ID，无法通过 MXU API 执行")

        run_name = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000:06d}"
        run_dir = self.output_root / "runs" / run_name
        run_dir.mkdir(parents=True, exist_ok=True)
        log_handle = (run_dir / "runtime.log").open("wb")
        command = self._command(root)
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        creationflags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        try:
            process = self._popen_factory(
                command,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                cwd=str(root),
                creationflags=creationflags,
            )
        except Exception as exc:
            log_handle.close()
            with self._lock:
                self._status = "error"
                self._last_error = str(exc)
            self._log("error", f"无法启动 MaaEnd: {exc}")
            raise RuntimeError(f"无法启动 MaaEnd: {exc}") from exc
        with self._lock:
            self._process = process
            self._process_owned = True
            self._log_handle = log_handle
            self._running = True
            self._status = "running"
            self._stop_event.clear()
            self._device = device
            self._last_run_dir = str(run_dir)
            self._last_exit_code = None
            self._last_error = ""
            self._started_at = time.time()
            self._completed_at = None
            self._api_base_url = ""
            self._api_instance_id = instance_id
            self._api_phase = "starting_service"
            self._api_maafw_version = ""
            self._api_instance_state = {}
            self._api_tasks = task_metadata
            self._api_task_ids = []
            self._api_resource_paths = resource_paths
            self._api_updated_at = self._started_at
        self._log("starting_service", "正在启动 MaaEnd v2.20 本地 MXU API")
        try:
            base_url, maafw_version, owned = self._discover_api(root, process)
            with self._lock:
                self._api_base_url = base_url
                self._api_maafw_version = maafw_version
                self._process_owned = owned
            self._submit_api_tasks(
                process,
                root,
                interface,
                config,
                profile,
                task_requests,
                task_metadata,
                resource_paths,
            )
        except Exception as exc:
            self._stop_event.set()
            self._stop_api_instance()
            with self._lock:
                owned = self._process_owned
            exit_code = self._terminate_process(process, owned=owned)
            error = f"MaaEnd MXU API 启动失败: {exc}"
            self._finalize_process(
                process,
                exit_code,
                log_handle,
                status="error",
                error=error,
            )
            raise RuntimeError(error) from exc
        self._log(
            "running",
            f"已在 USB 真机 {device} 提交 MaaEnd 实例“{instance_name}”的全部任务",
        )
        watcher = threading.Thread(
            target=self._watch_api_run,
            args=(process, log_handle),
            daemon=True,
            name="maaend-mxu-api-watcher",
        )
        watcher.start()
        return self.snapshot()

    def stop(self) -> dict[str, object]:
        with self._lock:
            process = self._process
            log_handle = self._log_handle
            if process is None:
                self._running = False
                if self._status in {"running", "stopping"}:
                    self._status = "stopped"
                return self.snapshot()
            self._status = "stopping"
            self._api_phase = "stopping"
            self._api_updated_at = time.time()
            owned = self._process_owned
            self._stop_event.set()
        self._log("stopping", "正在通过 MXU API 停止 MaaEnd ADB 任务")
        self._stop_api_instance()
        exit_code = self._terminate_process(process, owned=owned)
        self._finalize_process(
            process,
            exit_code,
            log_handle,
            status="stopped",
        )
        return self.snapshot()

    def _runtime_output_snapshot(self) -> dict[str, object]:
        run_dir = Path(self._last_run_dir) if self._last_run_dir else None
        if run_dir is None:
            return {"path": "", "available": False, "lines": []}
        return _tail_text_file(run_dir / "runtime.log")

    def _upstream_debug_snapshot(self) -> dict[str, object]:
        debug_root = self.runtime_path / "debug"
        if not debug_root.is_dir():
            return {"path": "", "available": False, "lines": []}
        try:
            candidates = sorted(
                (
                    item
                    for item in debug_root.glob("*.log")
                    if item.is_file()
                ),
                key=lambda item: item.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError:
            candidates = []
        if not candidates:
            return {"path": "", "available": False, "lines": []}
        return _tail_text_file(candidates[0])

    def snapshot(self) -> dict[str, object]:
        available, upstream, install_error = self._refresh_install_status()
        game_catalog = self._game_catalog_snapshot(available)
        profiles, profile_catalog_error = self._configured_profiles_snapshot(
            available
        )
        with self._lock:
            running = self._running
            status = "running" if running else self._status
            last_error = self._last_error or install_error
            profile = dict(self._last_profile or {})
            configured_profile = next(
                (
                    dict(row)
                    for row in profiles
                    if str(row.get("name") or "") == self.instance_name
                ),
                {},
            )
            instance_options = [
                {
                    "value": str(row.get("name") or ""),
                    "label": (
                        f"{row.get('name') or '未命名实例'} · "
                        f"{int(row.get('task_count') or 0)} 项任务"
                    ),
                }
                for row in profiles
                if row.get("controller") == "ADB" and row.get("name")
            ]
            if self.instance_name and not any(
                option["value"] == self.instance_name
                for option in instance_options
            ):
                instance_options.insert(
                    0,
                    {
                        "value": self.instance_name,
                        "label": f"{self.instance_name} · 未找到",
                    },
                )
            return {
                "adapter_id": MAAEND_ADAPTER_ID,
                "standard_version": MAAEND_STANDARD_VERSION,
                "status": status,
                "running": running,
                "available": available,
                "device": self._device,
                "upstream": {
                    **upstream,
                    "disk_bytes": self._disk_bytes if available else 0,
                    "disk_mib": round(self._disk_bytes / 1024 / 1024, 1)
                    if available
                    else 0,
                },
                "preflight": self._last_preflight,
                "profile": profile,
                "configured_profile": configured_profile,
                "profiles": profiles,
                "profile_config_error": profile_catalog_error,
                "game_catalog": game_catalog,
                "runtime_options": [
                    {
                        "id": "runtime_path",
                        "type": "text",
                        "label": "MaaEnd 发布目录",
                        "description": f"解压后的官方 Windows {MAAEND_STANDARD_VERSION} 发布包根目录",
                        "value": str(self.runtime_path),
                        "required": True,
                    },
                    {
                        "id": "instance_name",
                        "type": "select" if instance_options else "text",
                        "label": "MaaEnd 实例名",
                        "description": "本项目只改写专用 ADB 实例；也可预检已有安全实例",
                        "value": self.instance_name,
                        "required": True,
                        "options": instance_options,
                    },
                ],
                "capabilities": {
                    "preflight": True,
                    "configure": True,
                    "start": True,
                    "stop": True,
                    "screenshot": False,
                    "configure_when_unavailable": True,
                    "adb_state_probe": bool(self.adb),
                    "mxu_http_status": True,
                },
                "boundary": {
                    "external_runtime": True,
                    "trusted_upstream_code": True,
                    "fixed_executable": "MaaEnd.exe",
                    "arbitrary_arguments": False,
                    "task_truth_source": "MXU GET /api/maa/state",
                },
                "last_error": last_error,
                "last_run_dir": self._last_run_dir,
                "last_exit_code": self._last_exit_code,
                "started_at": self._started_at,
                "completed_at": self._completed_at,
                "managed_instance": {
                    "id": MAAEND_MANAGED_INSTANCE_ID,
                    "name": MAAEND_MANAGED_INSTANCE_NAME,
                },
                "mxu_api": {
                    "base_url": self._api_base_url,
                    "phase": self._api_phase,
                    "instance_id": self._api_instance_id,
                    "maafw_version": self._api_maafw_version,
                    "resource_paths": list(self._api_resource_paths),
                    "task_ids": list(self._api_task_ids),
                    "tasks": json.loads(json.dumps(self._api_tasks)),
                    "instance_state": json.loads(
                        json.dumps(self._api_instance_state)
                    ),
                    "updated_at": self._api_updated_at,
                    "truth_source": "GET /api/maa/state",
                },
                "runtime_output": self._runtime_output_snapshot(),
                "upstream_debug": self._upstream_debug_snapshot()
                if available
                else {"path": "", "available": False, "lines": []},
                "logs": list(self._logs),
            }

    def close(self) -> None:
        try:
            self.stop()
        except Exception:
            pass
