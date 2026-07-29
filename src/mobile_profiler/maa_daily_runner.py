#!/usr/bin/env python3
"""Run the enabled MAA daily queue unattended on one Android device."""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from mobile_profiler.maa_iteration import (
    EvidenceImageWriter,
    callback_summary,
    sha256_file,
    write_json_atomic,
)


ASST_OPTION_TOUCH_MODE = 2
ASST_OPTION_CLIENT_TYPE = 6
ASST_OPTION_VIEWPORT = 7
INT_MAX = 2_147_483_647
DAILY_TASKS = ("StartUp", "Fight", "Infrast", "Recruit", "Mall", "Award")
INFRAST_FACILITIES = (
    "Mfg",
    "Trade",
    "Control",
    "Power",
    "Reception",
    "Office",
    "Dorm",
    "Processing",
    "Training",
)
ACCOUNT_MUTATING_TASKS = frozenset(DAILY_TASKS[1:])
CRITICAL_INFRAST_SUBTASKS = frozenset(
    {
        "InfrastInfoTask",
        "InfrastMfgTask",
        "InfrastTradeTask",
        "InfrastControlTask",
        "InfrastPowerTask",
        "InfrastReceptionTask",
        "InfrastOfficeTask",
        "InfrastDormTask",
        "InfrastTrainingTask",
        "InfrastProcessingTask",
    }
)
OPTIONAL_PROCESS_PROBE_SIGNATURES = frozenset(
    {
        ("Infrast", "ProcessTask", ("UnlockClues",)),
        ("Infrast", "ProcessTask", ("EndOfClueExchange",)),
        ("Infrast", "ProcessTask", ("InfrastClueSelfFull",)),
        ("Infrast", "ProcessTask", ("InfrastReceptionReceiveMessageBoard",)),
        (
            "Infrast",
            "ProcessTask",
            (
                "InfrastClueSelfNew",
                "InfrastClueSelfMaybeFull",
                "ReceptionFlag",
            ),
        ),
        ("Mall", "ProcessTask", ("CreditShop-NoMoney",)),
        ("Mall", "ProcessTask", ("CreditShop-BuyIt",)),
    }
)
MALL_SHOPPING_DEFAULTS = {
    "Official": {
        "buy_first": ["招聘许可"],
        "blacklist": ["碳", "家具零件", "加急许可"],
    },
    "Bilibili": {
        "buy_first": ["招聘许可"],
        "blacklist": ["碳", "家具零件", "加急许可"],
    },
    "YoStarEN": {
        "buy_first": ["Recruitment Permit"],
        "blacklist": ["Carbon", "Furniture Part", "Expedited Plan"],
    },
    "YoStarJP": {
        "buy_first": ["求人票"],
        "blacklist": ["炭素", "家具パーツ", "緊急招集票"],
    },
    "YoStarKR": {
        "buy_first": ["모집 허가증"],
        "blacklist": ["카본", "가구 부품", "즉시 완료 허가증"],
    },
    "txwy": {
        "buy_first": ["招聘許可"],
        "blacklist": ["碳", "傢俱零件", "加急許可"],
    },
}
CLIENT_PACKAGES = {
    "Official": "com.hypergryph.arknights",
    "Bilibili": "com.hypergryph.arknights.bilibili",
    "YoStarEN": "com.YoStarEN.Arknights",
    "YoStarJP": "com.YoStarJP.Arknights",
    "YoStarKR": "com.YoStarKR.Arknights",
    "txwy": "tw.txwy.and.arknights",
}
MESSAGE_NAMES = {
    0: "InternalError",
    1: "InitFailed",
    2: "ConnectionInfo",
    3: "AllTasksCompleted",
    10000: "TaskChainError",
    10001: "TaskChainStart",
    10002: "TaskChainCompleted",
    10003: "TaskChainExtraInfo",
    10004: "TaskChainStopped",
    20000: "SubTaskError",
    20001: "SubTaskStart",
    20002: "SubTaskCompleted",
    20003: "SubTaskExtraInfo",
}


def _bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.replace("；", ";").split(";") if part.strip()]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _task_type(row: Mapping[str, object]) -> str:
    value = str(row.get("TaskType") or row.get("$type") or "").strip()
    return value[:-4] if value.endswith("Task") else value


def _legacy_value(path: Path, key: str, default: object) -> object:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        current = str(payload.get("Current") or "Default")
        profile = payload.get("Configurations", {}).get(current, {})
        if isinstance(profile, dict):
            return profile.get(key, default)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        pass
    return default


def default_daily_plan(*, client_type: str = "Official") -> list[dict[str, object]]:
    """Return conservative MAA defaults when a GUI queue has not been saved yet."""

    shopping_defaults = MALL_SHOPPING_DEFAULTS.get(
        client_type,
        MALL_SHOPPING_DEFAULTS["Official"],
    )

    return [
        {
            "task": "StartUp",
            "params": {"client_type": client_type, "start_game_enabled": True},
        },
        {
            "task": "Fight",
            "params": {
                "stage": "",
                "medicine": 0,
                "medicine_expire_days": 0,
                "stone": 0,
                "times": INT_MAX,
                "series": 0,
                "DrGrandet": False,
                "report_to_penguin": False,
                "report_to_yituliu": False,
                "server": "CN",
                "client_type": client_type,
            },
        },
        {
            "task": "Infrast",
            "params": {
                "facility": ["Mfg", "Trade", "Control", "Power", "Reception", "Office", "Dorm"],
                "drones": "Money",
                "continue_training": False,
                "threshold": 0.3,
                "dorm_notstationed_enabled": True,
                "dorm_trust_enabled": True,
                "replenish": True,
                "reception_message_board": True,
                "reception_clue_exchange": True,
                "reception_send_clue": True,
                "mode": 0,
            },
        },
        {
            "task": "Recruit",
            "params": {
                "refresh": True,
                "force_refresh": True,
                "select": [4],
                "confirm": [3, 4],
                "times": 4,
                "set_time": True,
                "expedite": False,
                "preserve_tags": [],
                "extra_tags_mode": 0,
                "first_tags": [],
                "recruitment_time": {"3": 540, "4": 540, "5": 540, "6": 540},
                "report_to_penguin": False,
                "report_to_yituliu": False,
                "server": "CN",
            },
        },
        {
            "task": "Mall",
            "params": {
                "credit_fight": False,
                "formation_index": 0,
                "visit_friends": True,
                "shopping": True,
                "buy_first": list(shopping_defaults["buy_first"]),
                "blacklist": list(shopping_defaults["blacklist"]),
                "force_shopping_if_credit_full": False,
                "only_buy_discount": False,
                "reserve_max_credit": False,
            },
        },
        {
            "task": "Award",
            "params": {
                "award": True,
                "mail": False,
                "recruit": False,
                "orundum": False,
                "mining": False,
                "specialaccess": False,
            },
        },
    ]


def override_daily_task_options(
    plan: Sequence[Mapping[str, object]],
    raw_options: object,
) -> list[dict[str, object]]:
    """Merge a validated, task-keyed option object into a selected daily plan."""

    if raw_options in (None, ""):
        return [
            {**dict(row), "params": dict(row.get("params", {}))}
            for row in plan
        ]
    if isinstance(raw_options, str):
        try:
            decoded = json.loads(raw_options)
        except json.JSONDecodeError as exc:
            raise ValueError("--task-options must be valid JSON") from exc
    else:
        decoded = raw_options
    if not isinstance(decoded, Mapping):
        raise ValueError("--task-options must be a task-keyed JSON object")

    allowed_params = {
        str(row["task"]): set(dict(row.get("params", {})))
        for row in default_daily_plan()
    }
    for task, values in decoded.items():
        task_name = str(task)
        if task_name not in allowed_params:
            raise ValueError(f"unsupported daily task options: {task_name}")
        if not isinstance(values, Mapping):
            raise ValueError(f"daily task options for {task_name} must be an object")
        unknown = set(str(key) for key in values) - allowed_params[task_name]
        if unknown:
            raise ValueError(
                f"unsupported {task_name} option(s): {', '.join(sorted(unknown))}"
            )

    result: list[dict[str, object]] = []
    for row in plan:
        task = str(row["task"])
        params = dict(row.get("params", {}))
        values = decoded.get(task, {})
        if isinstance(values, Mapping):
            params.update(dict(values))
        result.append({**dict(row), "params": params})
    return result


def _convert_gui_task(
    task: str,
    row: Mapping[str, object],
    *,
    client_type: str,
) -> dict[str, object]:
    if task == "StartUp":
        return {"client_type": client_type, "start_game_enabled": True}
    if task == "Fight":
        stage_plan = row.get("StagePlan")
        stage = ""
        if isinstance(stage_plan, Sequence) and not isinstance(stage_plan, (bytes, bytearray, str)):
            stage = str(next(iter(stage_plan), "") or "")
        return {
            "stage": stage,
            "medicine": _int(row.get("MedicineCount")) if _bool(row.get("UseMedicine")) else 0,
            "medicine_expire_days": (
                max(0, _int(row.get("MedicineExpireDays"), 2))
                if _bool(row.get("UseExpiringMedicine"))
                else 0
            ),
            "stone": _int(row.get("StoneCount")) if _bool(row.get("UseStone")) else 0,
            "times": (
                max(0, _int(row.get("TimesLimit"), INT_MAX))
                if _bool(row.get("EnableTimesLimit"))
                else INT_MAX
            ),
            "series": _int(row.get("Series"), 1),
            "DrGrandet": _bool(row.get("IsDrGrandet")),
            "report_to_penguin": False,
            "report_to_yituliu": False,
            "server": "CN",
            "client_type": client_type,
        }
    if task == "Infrast":
        facilities: list[str] = []
        room_list = row.get("RoomList")
        if isinstance(room_list, Sequence) and not isinstance(room_list, (bytes, bytearray, str)):
            for room in room_list:
                if not isinstance(room, Mapping) or not _bool(room.get("IsEnabled"), True):
                    continue
                name = str(room.get("Room") or "").strip()
                if name:
                    facilities.append(name)
        mode_name = str(row.get("Mode") or "Normal")
        mode = {"Normal": 0, "Custom": 10_000, "Rotation": 20_000}.get(
            mode_name, _int(row.get("Mode"), 0)
        )
        params: dict[str, object] = {
            "facility": facilities,
            "drones": str(row.get("UsesOfDrones") or "_NotUse"),
            "continue_training": _bool(row.get("ContinueTraining")),
            "threshold": max(0.0, min(1.0, _int(row.get("DormThreshold"), 30) / 100.0)),
            "dorm_notstationed_enabled": _bool(row.get("DormFilterNotStationed")),
            "dorm_trust_enabled": _bool(row.get("DormTrustEnabled")),
            "replenish": _bool(row.get("OriginiumShardAutoReplenishment")),
            "reception_message_board": _bool(row.get("ReceptionMessageBoard"), True),
            "reception_clue_exchange": _bool(row.get("ReceptionClueExchange"), True),
            "reception_send_clue": _bool(row.get("SendClue"), True),
            "mode": mode,
        }
        if mode == 10_000:
            params["filename"] = str(row.get("Filename") or "")
            params["plan_index"] = max(0, _int(row.get("PlanSelect"), 0))
        return params
    if task == "Recruit":
        select: list[int] = []
        confirm: list[int] = []
        if _bool(row.get("Level3Choose")):
            confirm.append(3)
        for level in (4, 5, 6):
            if _bool(row.get(f"Level{level}Choose")):
                select.append(level)
                confirm.append(level)
        preserve_tags = _strings(row.get("PreserveTagList")) if _bool(row.get("PreserveTagEnabled")) else []
        first_tags = _strings(row.get("Level3PreferTags")) if _bool(row.get("PreferTagEnabled")) else []
        expedite = _bool(row.get("UseExpedited"))
        params = {
            "refresh": _bool(row.get("RefreshLevel3")),
            "force_refresh": _bool(row.get("ForceRefresh"), True),
            "select": select,
            "confirm": confirm,
            "times": max(0, _int(row.get("MaxTimes"), 4)),
            "set_time": True,
            "expedite": expedite,
            "preserve_tags": preserve_tags,
            "extra_tags_mode": _int(row.get("ExtraTagMode"), 0),
            "first_tags": first_tags,
            "recruitment_time": {
                "3": _int(row.get("Level3Time"), 540),
                "4": _int(row.get("Level4Time"), 540),
                "5": 540,
                "6": 540,
            },
            "report_to_penguin": False,
            "report_to_yituliu": False,
            "server": "CN",
        }
        if expedite:
            params["expedite_times"] = max(0, _int(row.get("MaxTimes"), 4))
        return params
    if task == "Mall":
        return {
            "credit_fight": _bool(row.get("IsCreditFightAvailable")),
            "formation_index": max(0, _int(row.get("CreditFightFormation"), 0)),
            "visit_friends": _bool(row.get("IsVisitFriendsAvailable"), _bool(row.get("VisitFriends"), True)),
            "shopping": _bool(row.get("Shopping"), True),
            "buy_first": _strings(row.get("FirstList")),
            "blacklist": _strings(row.get("BlackList")),
            "force_shopping_if_credit_full": _bool(row.get("ShoppingIgnoreBlackListWhenFull")),
            "only_buy_discount": _bool(row.get("OnlyBuyDiscount")),
            "reserve_max_credit": _bool(row.get("ReserveMaxCredit")),
        }
    if task == "Award":
        return {
            "award": _bool(row.get("Award"), True),
            "mail": _bool(row.get("Mail")),
            "recruit": _bool(row.get("FreeGacha")),
            "orundum": _bool(row.get("Orundum")),
            "mining": _bool(row.get("Mining")),
            "specialaccess": _bool(row.get("SpecialAccess")),
        }
    raise ValueError(f"unsupported daily task: {task}")


def load_gui_daily_plan(path: Path) -> list[dict[str, object]]:
    """Translate the enabled MaaWpfGui queue into MaaCore task parameters."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    current = str(payload.get("Current") or "Default")
    configurations = payload.get("Configurations")
    if not isinstance(configurations, dict) or not isinstance(configurations.get(current), dict):
        raise ValueError(f"MAA GUI profile {current!r} is missing from {path}")
    profile = configurations[current]
    queue = profile.get("TaskQueue")
    if not isinstance(queue, list):
        raise ValueError(f"MAA GUI TaskQueue is missing from {path}")

    legacy_path = path.with_name("gui.json")
    client_type = str(_legacy_value(legacy_path, "Start.ClientType", "Official") or "Official")
    converted: list[dict[str, object]] = []
    for raw_row in queue:
        if not isinstance(raw_row, dict) or not _bool(raw_row.get("IsEnable"), True):
            continue
        task = _task_type(raw_row)
        if task not in DAILY_TASKS:
            continue
        converted.append(
            {
                "task": task,
                "params": _convert_gui_task(task, raw_row, client_type=client_type),
            }
        )

    if not any(row["task"] == "StartUp" for row in converted):
        converted.insert(
            0,
            {
                "task": "StartUp",
                "params": {"client_type": client_type, "start_game_enabled": True},
            },
        )
    if not any(row["task"] in ACCOUNT_MUTATING_TASKS for row in converted):
        raise ValueError("MAA GUI queue does not contain an enabled daily task")
    return converted


def resolve_daily_plan(runtime_root: Path, gui_config: Path | None) -> tuple[list[dict[str, object]], str]:
    path = (gui_config or runtime_root / "config" / "gui.new.json").expanduser().resolve()
    if path.is_file():
        return load_gui_daily_plan(path), os.fspath(path)
    return default_daily_plan(), "built_in"


def select_daily_plan(
    plan: Sequence[Mapping[str, object]],
    tasks: str | None,
) -> list[dict[str, object]]:
    """Select an ordered task subset without silently inventing missing tasks."""

    if not tasks or tasks.strip().lower() == "all":
        return [{"task": str(row["task"]), "params": dict(row.get("params", {}))} for row in plan]
    requested = [part.strip() for part in re.split(r"[,;]", tasks) if part.strip()]
    if not requested:
        raise ValueError("--tasks did not contain a task name")
    duplicates = sorted({task for task in requested if requested.count(task) > 1})
    if duplicates:
        raise ValueError(f"--tasks contains duplicates: {', '.join(duplicates)}")
    unknown = [task for task in requested if task not in DAILY_TASKS]
    if unknown:
        raise ValueError(f"unsupported daily tasks: {', '.join(unknown)}")
    available = {str(row["task"]): row for row in plan}
    missing = [task for task in requested if task not in available]
    if missing:
        raise ValueError(f"selected tasks are disabled or absent from the GUI queue: {', '.join(missing)}")
    return [
        {"task": task, "params": dict(available[task].get("params", {}))}
        for task in requested
    ]


def override_infrast_facilities(
    plan: Sequence[Mapping[str, object]],
    facilities: str | None,
) -> list[dict[str, object]]:
    """Narrow an Infrast run for diagnosis without mutating the source plan."""

    copied = [
        {"task": str(row["task"]), "params": dict(row.get("params", {}))}
        for row in plan
    ]
    if not facilities:
        return copied
    requested = [part.strip() for part in re.split(r"[,;]", facilities) if part.strip()]
    if not requested:
        raise ValueError("--infrast-facilities did not contain a facility name")
    duplicates = sorted({name for name in requested if requested.count(name) > 1})
    if duplicates:
        raise ValueError(f"--infrast-facilities contains duplicates: {', '.join(duplicates)}")
    unknown = [name for name in requested if name not in INFRAST_FACILITIES]
    if unknown:
        raise ValueError(f"unsupported Infrast facilities: {', '.join(unknown)}")
    infrast = next((row for row in copied if row["task"] == "Infrast"), None)
    if infrast is None:
        raise ValueError("--infrast-facilities requires Infrast in the selected task plan")
    params = infrast["params"]
    if not isinstance(params, dict):
        raise ValueError("Infrast task parameters are invalid")
    params["facility"] = requested
    return copied


def task_run_successful(task: str, run: Mapping[str, object]) -> bool:
    """Treat completed-but-degraded base shifts as failures that need retry."""

    if run.get("status") != "completed" or not _bool(run.get("all_tasks_completed")):
        return False
    if _bool(run.get("timed_out")) or _bool(run.get("no_progress")) or run.get("runner_error"):
        return False
    errors = run.get("errors")
    if task != "Infrast" or not isinstance(errors, list):
        return True
    return not any(
        isinstance(error, Mapping) and error.get("subtask") in CRITICAL_INFRAST_SUBTASKS
        for error in errors
    )


def subtask_error_summary(details: object) -> dict[str, object]:
    """Classify MAA's expected negative probes without hiding their evidence."""

    summary = callback_summary("SubTaskError", details)
    first = summary.get("first")
    probe_sequence = (
        tuple(str(item) for item in first)
        if isinstance(first, Sequence) and not isinstance(first, (bytes, bytearray, str))
        else ()
    )
    probe_signature = (
        str(summary.get("taskchain") or ""),
        str(summary.get("subtask") or ""),
        probe_sequence,
    )
    optional = probe_signature in OPTIONAL_PROCESS_PROBE_SIGNATURES
    # MaaCore does not expose a node/phase when the optional buy-first pass has
    # no matching commodity. Keep this protocol-level exception scoped to the
    # exact Mall subtask; newer integrations should emit a phase signature.
    optional = optional or (
        summary.get("taskchain") == "Mall"
        and summary.get("subtask") == "CreditShoppingTask"
    )
    summary["optional"] = optional
    return summary


def known_recovery_fingerprint(run: Mapping[str, object]) -> str | None:
    """Return a deterministic restart signature that does not need model triage."""

    if run.get("task") != "Recruit":
        return None
    errors = run.get("errors")
    if not isinstance(errors, list) or not errors:
        return None

    saw_confirm = False
    saw_recruit_error = False
    for value in errors:
        if not isinstance(value, Mapping) or value.get("taskchain") != "Recruit":
            return None
        subtask = value.get("subtask")
        first = value.get("first")
        if subtask == "ProcessTask" and first == ["RecruitConfirm"]:
            saw_confirm = True
            continue
        what = str(value.get("what") or "")
        if subtask == "AutoRecruitTask" and what in {"", "RecruitError"}:
            saw_recruit_error = saw_recruit_error or what == "RecruitError"
            continue
        return None
    if saw_confirm and saw_recruit_error:
        return "recruit:recognition:confirm-and-auto-recruit-error"
    return None


def qwen_recovery_action(
    diagnosis: Mapping[str, object] | None,
    *,
    default: str = "restart",
) -> str:
    """Accept only the two non-interactive recovery actions exposed to Qwen."""

    if default not in {"restart", "stop"}:
        raise ValueError("default Qwen recovery action must be restart or stop")
    if not isinstance(diagnosis, Mapping) or diagnosis.get("available") is not True:
        return default
    analysis = diagnosis.get("analysis")
    if not isinstance(analysis, Mapping):
        return default
    action = str(analysis.get("safe_action") or "").strip().lower()
    return action if action in {"restart", "stop"} else default


def _utf8(value: str | Path) -> bytes:
    return os.fspath(value).encode("utf-8")


def _run_adb(
    adb: Path,
    address: str,
    *arguments: str,
    run_func: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[bytes]:
    return run_func(
        [os.fspath(adb), "-s", address, *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def prepare_device(
    adb: Path,
    address: str,
    *,
    run_func: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> dict[str, object]:
    """Wake and dismiss an insecure keyguard without requiring a human."""

    actions: list[str] = []
    state = _run_adb(adb, address, "get-state", run_func=run_func)
    if state.returncode != 0 or state.stdout.strip() != b"device":
        raise RuntimeError(f"ADB device is unavailable: {state.stdout.decode('utf-8', 'replace').strip()}")

    power = _run_adb(adb, address, "shell", "dumpsys", "power", run_func=run_func)
    power_text = power.stdout.decode("utf-8", "replace")
    if "mWakefulness=Awake" not in power_text:
        wake = _run_adb(adb, address, "shell", "input", "keyevent", "224", run_func=run_func)
        if wake.returncode != 0:
            raise RuntimeError("failed to wake Android device")
        actions.append("wake")
        time.sleep(0.8)

    _run_adb(adb, address, "shell", "wm", "dismiss-keyguard", run_func=run_func)
    window = _run_adb(adb, address, "shell", "dumpsys", "window", run_func=run_func)
    window_text = window.stdout.decode("utf-8", "replace")
    if "mDreamingLockscreen=true" in window_text:
        size = _run_adb(adb, address, "shell", "wm", "size", run_func=run_func)
        match = re.search(rb"Physical size:\s*(\d+)x(\d+)", size.stdout)
        width, height = (int(match.group(1)), int(match.group(2))) if match else (1080, 2400)
        swipe = _run_adb(
            adb,
            address,
            "shell",
            "input",
            "swipe",
            str(width // 2),
            str(int(height * 0.85)),
            str(width // 2),
            str(int(height * 0.2)),
            "500",
            run_func=run_func,
        )
        if swipe.returncode != 0:
            raise RuntimeError("failed to dismiss Android keyguard")
        actions.append("swipe_unlock")
        time.sleep(1.0)
        _run_adb(adb, address, "shell", "wm", "dismiss-keyguard", run_func=run_func)

    final_power = _run_adb(adb, address, "shell", "dumpsys", "power", run_func=run_func)
    final_window = _run_adb(adb, address, "shell", "dumpsys", "window", run_func=run_func)
    final_power_text = final_power.stdout.decode("utf-8", "replace")
    final_window_text = final_window.stdout.decode("utf-8", "replace")
    if "mWakefulness=Awake" not in final_power_text:
        raise RuntimeError("Android device remained asleep after wake recovery")
    if "mDreamingLockscreen=true" in final_window_text:
        raise RuntimeError("Android keyguard requires credentials; unattended unlock is unavailable")
    return {"awake": True, "unlocked": True, "actions": actions}


def set_device_stay_awake(adb: Path, address: str, enabled: bool) -> bool:
    result = _run_adb(
        adb,
        address,
        "shell",
        "svc",
        "power",
        "stayon",
        "true" if enabled else "false",
    )
    return result.returncode == 0


def restart_game(adb: Path, address: str, package: str) -> dict[str, object]:
    """Reset game UI state without clicking through an unknown confirmation."""

    result = _run_adb(adb, address, "shell", "am", "force-stop", package)
    output = result.stdout.decode("utf-8", "replace").strip()
    if result.returncode != 0:
        raise RuntimeError(f"failed to stop {package}: {output}")
    time.sleep(1.0)
    return {"action": "force_stop", "package": package, "output": output or None}


def _model_json(text: str) -> object:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return {"raw": text}


class QwenRecoveryAdvisor:
    """Optional vision triage; its output is never allowed to click or confirm."""

    def __init__(self, base_url: str, model: str, timeout: float) -> None:
        root = base_url.rstrip("/")
        self.url = f"{root}/chat/completions" if root.endswith("/v1") else f"{root}/v1/chat/completions"
        self.model = model
        self.timeout = timeout
        self.disabled_reason = ""

    def analyze(self, image: bytes | None, task: str, errors: Sequence[object]) -> dict[str, object]:
        if self.disabled_reason:
            return {"available": False, "error": self.disabled_reason}
        if not image:
            return {"available": False, "error": "no screenshot was available"}
        prompt = (
            "You are a safety-limited Arknights MAA recovery observer. Classify the screenshot and explain "
            "why the task failed. Return one compact JSON object with keys page, blocking_ui, diagnosis, "
            "and safe_action. safe_action must be exactly restart or stop. Never recommend clicking, "
            "confirming, spending currency, using medicine/Originium, refreshing tags, or changing account "
            f"state. Failed task: {task}. Recent errors: {json.dumps(list(errors)[-8:], ensure_ascii=False)}"
        )
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 350,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64," + base64.b64encode(image).decode("ascii")
                            },
                        },
                    ],
                }
            ],
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
            content = response_payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(
                    str(part.get("text", "")) if isinstance(part, Mapping) else str(part)
                    for part in content
                )
            return {"available": True, "model": self.model, "analysis": _model_json(str(content))}
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.disabled_reason = f"{type(exc).__name__}: {exc}"
            return {"available": False, "error": self.disabled_reason}


def _capture(core: ctypes.WinDLL, handle: int) -> bytes | None:
    async_id = core.AsstAsyncScreencap(handle, 1)
    if async_id <= 0:
        return None
    buffer = ctypes.create_string_buffer(16 * 1024 * 1024)
    size = core.AsstGetImage(handle, buffer, len(buffer))
    if size <= 0 or size > len(buffer):
        return None
    payload = bytes(buffer.raw[:size])
    return payload if payload.startswith(b"\x89PNG\r\n\x1a\n") else None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--address", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--gui-config", type=Path)
    parser.add_argument("--tasks", help="comma-separated ordered subset, for example Infrast,Recruit,Mall,Award")
    parser.add_argument(
        "--task-options",
        help="JSON object with per-task MaaCore parameter overrides",
    )
    parser.add_argument(
        "--infrast-facilities",
        help="comma-separated Infrast subset for focused verification, for example Power,Reception",
    )
    parser.add_argument("--viewport", default="adaptive")
    parser.add_argument("--config", default="General")
    parser.add_argument("--max-seconds", type=float, default=3 * 60 * 60)
    parser.add_argument("--no-progress-seconds", type=float, default=5 * 60)
    parser.add_argument("--snapshot-seconds", type=float, default=60.0)
    parser.add_argument("--task-retries", type=int, default=1)
    parser.add_argument("--recovery-retries", type=int, default=2)
    parser.add_argument("--app-package")
    parser.add_argument("--qwen-url", default=os.environ.get("MAA_QWEN_URL", "http://192.168.31.237:8000"))
    parser.add_argument("--qwen-model", default=os.environ.get("MAA_QWEN_MODEL", "qwen3.6-27b"))
    parser.add_argument("--qwen-timeout", type=float, default=8.0)
    parser.add_argument("--no-qwen", action="store_true")
    parser.add_argument("--no-keep-awake", action="store_true")
    parser.add_argument("--allow-account-mutation", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    core_root = args.core_root.expanduser().resolve()
    runtime_root = args.runtime_root.expanduser().resolve()
    adb_path = args.adb.expanduser().resolve()
    run_dir = args.run_dir.expanduser().resolve()
    core_path = core_root / "MaaCore.dll"
    if not core_path.is_file():
        raise RuntimeError(f"MaaCore.dll not found: {core_path}")
    if not (runtime_root / "resource").is_dir():
        raise RuntimeError(f"resource directory not found: {runtime_root / 'resource'}")
    if not adb_path.is_file():
        raise RuntimeError(f"adb executable not found: {adb_path}")
    if args.max_seconds <= 0 or args.no_progress_seconds <= 0:
        raise RuntimeError("time limits must be positive")
    if args.task_retries < 0 or args.recovery_retries < 0:
        raise RuntimeError("retry counts cannot be negative")
    if args.qwen_timeout <= 0:
        raise RuntimeError("--qwen-timeout must be positive")
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"run directory is not empty: {run_dir}")

    full_plan, plan_source = resolve_daily_plan(runtime_root, args.gui_config)
    plan = select_daily_plan(full_plan, args.tasks)
    plan = override_infrast_facilities(plan, args.infrast_facilities)
    plan = override_daily_task_options(plan, args.task_options)
    if any(row["task"] in ACCOUNT_MUTATING_TASKS for row in plan) and not args.allow_account_mutation:
        raise RuntimeError("daily tasks change account state; pass --allow-account-mutation explicitly")

    startup_row = next(
        (row for row in full_plan if row["task"] == "StartUp"),
        default_daily_plan()[0],
    )
    startup_params = dict(startup_row.get("params", {}))
    client_type = str(startup_params.get("client_type") or "Official")
    package = str(args.app_package or CLIENT_PACKAGES.get(client_type) or "")
    if not package:
        raise RuntimeError(f"unknown package for client type {client_type!r}; pass --app-package")

    run_dir.mkdir(parents=True, exist_ok=True)
    user_dir = run_dir / "user"
    user_dir.mkdir(parents=True, exist_ok=True)
    device_preparation = prepare_device(adb_path, args.address)
    keep_awake_enabled = False
    if not args.no_keep_awake:
        keep_awake_enabled = set_device_stay_awake(adb_path, args.address, True)
        device_preparation["keep_awake"] = keep_awake_enabled

    started_at = datetime.now().astimezone().isoformat()
    write_json_atomic(
        run_dir / "request.json",
        {
            "schema_version": 2,
            "kind": "maa_daily",
            "plan_source": plan_source,
            "plan": plan,
            "selected_tasks": [row["task"] for row in plan],
            "viewport": args.viewport,
            "address": args.address,
            "max_seconds": args.max_seconds,
            "no_progress_seconds": args.no_progress_seconds,
            "task_retries": args.task_retries,
            "recovery_retries": args.recovery_retries,
            "recovery_policy": "force_stop_then_startup",
            "qwen": {
                "enabled": not args.no_qwen,
                "url": args.qwen_url,
                "model": args.qwen_model,
                "recovery_decisions_only": True,
                "allowed_actions": ["restart", "stop"],
                "can_control_game": False,
            },
            "started_at": started_at,
        },
    )
    write_json_atomic(
        run_dir / "environment.json",
        {
            "schema_version": 2,
            "core": {"path": os.fspath(core_path), "sha256": sha256_file(core_path)},
            "runtime": {"path": os.fspath(runtime_root)},
            "adb": {"path": os.fspath(adb_path), "sha256": sha256_file(adb_path)},
            "device": {
                "address": args.address,
                "preparation": device_preparation,
                "client_type": client_type,
                "package": package,
            },
        },
    )

    states: dict[str, dict[str, object]] = {
        str(row["task"]): {
            "task": row["task"],
            "status": "pending",
            "attempts": [],
            "recoveries": [],
            "errors": [],
            "optional_errors": [],
        }
        for row in plan
    }
    recovery_runs: list[dict[str, object]] = []
    runs_by_id: dict[int, dict[str, object]] = {}
    completion_events: dict[int, threading.Event] = {}
    last_images: dict[int, bytes] = {}
    event_lock = threading.Lock()
    event_file = (run_dir / "events.jsonl").open("a", encoding="utf-8")
    capture_requested = threading.Event()
    last_progress = [time.monotonic()]
    current_run: list[dict[str, object] | None] = [None]
    global_timed_out = [False]
    snapshot_index = 0
    image_writer = EvidenceImageWriter(run_dir)
    runner_error = ""
    recovery_blocked = False
    bootstrap_failed = False
    overall_started = time.monotonic()
    deadline = overall_started + args.max_seconds

    advisor = None
    if not args.no_qwen and args.qwen_url:
        advisor = QwenRecoveryAdvisor(args.qwen_url, args.qwen_model, args.qwen_timeout)

    dll_search = None
    core = None
    handle: int | None = None

    try:
        dll_search = os.add_dll_directory(os.fspath(core_root))
        core = ctypes.WinDLL(os.fspath(core_path))
        callback_type = ctypes.WINFUNCTYPE(None, ctypes.c_int32, ctypes.c_char_p, ctypes.c_void_p)
        core.AsstSetUserDir.argtypes = [ctypes.c_char_p]
        core.AsstSetUserDir.restype = ctypes.c_uint8
        core.AsstLoadResource.argtypes = [ctypes.c_char_p]
        core.AsstLoadResource.restype = ctypes.c_uint8
        core.AsstCreateEx.argtypes = [callback_type, ctypes.c_void_p]
        core.AsstCreateEx.restype = ctypes.c_void_p
        core.AsstDestroy.argtypes = [ctypes.c_void_p]
        core.AsstSetInstanceOption.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_char_p]
        core.AsstSetInstanceOption.restype = ctypes.c_uint8
        core.AsstConnect.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
        core.AsstConnect.restype = ctypes.c_uint8
        core.AsstConnected.argtypes = [ctypes.c_void_p]
        core.AsstConnected.restype = ctypes.c_uint8
        core.AsstAppendTask.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        core.AsstAppendTask.restype = ctypes.c_int32
        core.AsstStart.argtypes = [ctypes.c_void_p]
        core.AsstStart.restype = ctypes.c_uint8
        core.AsstStop.argtypes = [ctypes.c_void_p]
        core.AsstStop.restype = ctypes.c_uint8
        core.AsstRunning.argtypes = [ctypes.c_void_p]
        core.AsstRunning.restype = ctypes.c_uint8
        core.AsstAsyncScreencap.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
        core.AsstAsyncScreencap.restype = ctypes.c_int32
        core.AsstGetImage.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64]
        core.AsstGetImage.restype = ctypes.c_uint64

        @callback_type
        def callback(message: int, details_raw: bytes | None, _custom: int) -> None:
            try:
                details: object = json.loads(details_raw.decode("utf-8")) if details_raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                details = {"raw": repr(details_raw)}
            name = MESSAGE_NAMES.get(message, str(message))
            task_id = _int(details.get("taskid")) if isinstance(details, dict) else 0
            run = runs_by_id.get(task_id)
            record = {
                "timestamp": datetime.now().astimezone().isoformat(),
                "message_id": message,
                "message": name,
                "details": details,
            }
            error_summary = subtask_error_summary(details) if message == 20000 else None
            if error_summary is not None:
                record["summary"] = error_summary
            if run:
                record["run"] = {
                    "kind": run["kind"],
                    "attempt": run["attempt"],
                    "task": run["task"],
                }
            with event_lock:
                event_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                event_file.flush()

            if message in {3, 10000, 10001, 10002, 10004, 20000, 20001, 20002, 20003}:
                last_progress[0] = time.monotonic()
            if run and isinstance(details, dict):
                if message == 10001:
                    run["status"] = "running"
                    run["started_at"] = record["timestamp"]
                    current_run[0] = run
                    capture_requested.set()
                elif message == 10002:
                    run["status"] = "completed"
                    run["completed_at"] = record["timestamp"]
                    capture_requested.set()
                elif message in {10000, 10004}:
                    run["status"] = "failed" if message == 10000 else "stopped"
                    run["completed_at"] = record["timestamp"]
                    capture_requested.set()
                elif message == 20000:
                    target_name = (
                        "optional_errors"
                        if error_summary is not None and _bool(error_summary.get("optional"))
                        else "errors"
                    )
                    target = run.get(target_name)
                    if isinstance(target, list) and error_summary is not None:
                        target.append(error_summary)

            if message == 3 and isinstance(details, dict):
                finished = details.get("finished_tasks")
                ids = [_int(value) for value in finished] if isinstance(finished, list) else [task_id]
                for finished_id in ids:
                    finished_run = runs_by_id.get(finished_id)
                    if finished_run:
                        finished_run["all_tasks_completed"] = True
                    event = completion_events.get(finished_id)
                    if event:
                        event.set()

            if message in {10000, 10001, 10002, 10004}:
                print(json.dumps(callback_summary(name, details), ensure_ascii=False), flush=True)
            elif message == 20000 and error_summary is not None and not _bool(
                error_summary.get("optional")
            ):
                print(json.dumps(error_summary, ensure_ascii=False), flush=True)

        if not core.AsstSetUserDir(_utf8(user_dir)):
            raise RuntimeError("AsstSetUserDir failed")
        if not core.AsstLoadResource(_utf8(runtime_root)):
            raise RuntimeError("AsstLoadResource failed")
        cache_root = runtime_root / "cache"
        if (cache_root / "resource").is_dir() and not core.AsstLoadResource(_utf8(cache_root)):
            raise RuntimeError("AsstLoadResource failed for cache")
        handle = core.AsstCreateEx(callback, None)
        if not handle:
            raise RuntimeError("AsstCreateEx failed")
        for option, value in (
            (ASST_OPTION_TOUCH_MODE, "adb"),
            (ASST_OPTION_CLIENT_TYPE, client_type),
            (ASST_OPTION_VIEWPORT, args.viewport),
        ):
            if not core.AsstSetInstanceOption(handle, option, value.encode("utf-8")):
                raise RuntimeError(f"AsstSetInstanceOption failed for key {option}")
        if not core.AsstConnect(handle, _utf8(adb_path), args.address.encode(), args.config.encode()):
            raise RuntimeError("AsstConnect failed")
        if not core.AsstConnected(handle):
            raise RuntimeError("MaaCore did not remain connected")

        initial = _capture(core, handle)
        if initial:
            image_writer.write("initial.png", initial, force=True)

        def save_capture(label: str, task_id: int = 0, *, force: bool = False) -> bytes | None:
            nonlocal snapshot_index
            if not core or not handle:
                return None
            payload = _capture(core, handle)
            if not payload:
                return None
            snapshot_index += 1
            safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or "screen"
            image_writer.write(
                f"snapshot-{snapshot_index:04d}-{safe_label}.png",
                payload,
                force=force,
            )
            if task_id > 0:
                last_images[task_id] = payload
            return payload

        def execute_task(
            row: Mapping[str, object],
            *,
            kind: str,
            attempt: int,
        ) -> dict[str, object]:
            task = str(row["task"])
            params = dict(row.get("params", {}))
            run: dict[str, object] = {
                "task": task,
                "task_id": 0,
                "kind": kind,
                "attempt": attempt,
                "status": "pending",
                "errors": [],
                "optional_errors": [],
                "all_tasks_completed": False,
                "timed_out": False,
                "no_progress": False,
                "runner_error": None,
            }
            if time.monotonic() >= deadline:
                run["status"] = "failed"
                run["timed_out"] = True
                run["runner_error"] = "overall deadline reached before task start"
                global_timed_out[0] = True
                run["successful"] = False
                return run

            task_id = core.AsstAppendTask(
                handle,
                task.encode("utf-8"),
                json.dumps(params, ensure_ascii=False).encode("utf-8"),
            )
            run["task_id"] = task_id
            if task_id <= 0:
                run["status"] = "failed"
                run["runner_error"] = f"AsstAppendTask failed for {task}"
                run["successful"] = False
                return run

            runs_by_id[task_id] = run
            done = threading.Event()
            completion_events[task_id] = done
            if not core.AsstStart(handle):
                core.AsstStop(handle)
                run["status"] = "failed"
                run["runner_error"] = f"AsstStart failed for {task}"
                run["successful"] = False
                return run

            attempt_started = time.monotonic()
            last_progress[0] = attempt_started
            current_run[0] = run
            last_snapshot = 0.0
            stopped = False
            while core.AsstRunning(handle):
                now = time.monotonic()
                if now >= deadline:
                    run["timed_out"] = True
                    global_timed_out[0] = True
                    core.AsstStop(handle)
                    stopped = True
                    break
                if now - last_progress[0] >= args.no_progress_seconds:
                    run["no_progress"] = True
                    core.AsstStop(handle)
                    stopped = True
                    break
                if capture_requested.is_set() or (
                    args.snapshot_seconds > 0 and now - last_snapshot >= args.snapshot_seconds
                ):
                    capture_requested.clear()
                    save_capture(f"{kind}-{task}-attempt-{attempt}", task_id)
                    last_snapshot = time.monotonic()
                time.sleep(0.1)

            if stopped:
                stop_deadline = time.monotonic() + 30.0
                while core.AsstRunning(handle) and time.monotonic() < stop_deadline:
                    time.sleep(0.1)
            else:
                done.wait(5.0)

            final_image = save_capture(
                f"{kind}-{task}-attempt-{attempt}-final",
                task_id,
                force=True,
            )
            if final_image:
                last_images[task_id] = final_image
            run["elapsed_seconds"] = round(time.monotonic() - attempt_started, 1)
            if run["status"] in {"pending", "running"}:
                run["status"] = "stopped" if stopped else "failed"
                if not run.get("runner_error"):
                    run["runner_error"] = "task ended without a terminal callback"
            run["successful"] = task_run_successful(task, run)
            current_run[0] = None
            return run

        qwen_dir = run_dir / "qwen"

        def diagnose_failure(run: dict[str, object]) -> dict[str, object] | None:
            fingerprint = known_recovery_fingerprint(run)
            if fingerprint:
                run["recovery_fingerprint"] = fingerprint
                run["qwen_skipped_reason"] = "known_deterministic_restart"
                return None
            if advisor is None:
                run["qwen_skipped_reason"] = "qwen_disabled"
                return None
            diagnosis = advisor.analyze(
                last_images.get(_int(run.get("task_id"))),
                str(run["task"]),
                run.get("errors", []) if isinstance(run.get("errors"), list) else [],
            )
            qwen_dir.mkdir(parents=True, exist_ok=True)
            filename = (
                f"{run['kind']}-{run['task']}-attempt-{run['attempt']}-"
                f"task-{run['task_id']}.json"
            )
            write_json_atomic(qwen_dir / filename, diagnosis)
            run["qwen"] = diagnosis
            return diagnosis

        def perform_recovery(reason: str) -> dict[str, object]:
            recovery: dict[str, object] = {
                "reason": reason,
                "policy": "force_stop_then_startup",
                "successful": False,
                "attempts": [],
            }
            for recovery_attempt in range(1, args.recovery_retries + 2):
                if global_timed_out[0] or time.monotonic() >= deadline:
                    global_timed_out[0] = True
                    recovery["error"] = "overall deadline reached during recovery"
                    break
                restart = restart_game(adb_path, args.address, package)
                startup_run = execute_task(
                    {"task": "StartUp", "params": startup_params},
                    kind="recovery",
                    attempt=recovery_attempt,
                )
                recovery["attempts"].append({"restart": restart, "startup": startup_run})
                recovery_runs.append(startup_run)
                if _bool(startup_run.get("successful")):
                    recovery["successful"] = True
                    break
                diagnosis = diagnose_failure(startup_run)
                recovery_action = qwen_recovery_action(diagnosis)
                startup_run["recovery_action"] = recovery_action
                if recovery_action == "stop":
                    recovery["decision"] = "qwen_stop"
                    break
            return recovery

        if plan and plan[0]["task"] != "StartUp":
            bootstrap = perform_recovery("bootstrap selected task subset")
            if not _bool(bootstrap.get("successful")):
                bootstrap_failed = True
                recovery_blocked = True

        if not recovery_blocked:
            for row_index, row in enumerate(plan):
                task = str(row["task"])
                state = states[task]
                for attempt in range(1, args.task_retries + 2):
                    run = execute_task(row, kind="task", attempt=attempt)
                    attempts = state["attempts"]
                    if isinstance(attempts, list):
                        attempts.append(run)
                    state["task_id"] = run.get("task_id", 0)
                    errors = state["errors"]
                    if isinstance(errors, list) and isinstance(run.get("errors"), list):
                        errors.extend(run["errors"])
                    optional_errors = state["optional_errors"]
                    if isinstance(optional_errors, list) and isinstance(
                        run.get("optional_errors"), list
                    ):
                        optional_errors.extend(run["optional_errors"])
                    if _bool(run.get("successful")):
                        state["status"] = "completed"
                        state["completed_at"] = datetime.now().astimezone().isoformat()
                        break

                    state["status"] = "failed"
                    diagnosis = diagnose_failure(run)
                    recovery_action = qwen_recovery_action(diagnosis)
                    run["recovery_action"] = recovery_action
                    if recovery_action == "stop":
                        state["recovery_decision"] = "qwen_stop"
                        recovery_blocked = True
                        break
                    if global_timed_out[0]:
                        break
                    recovery = perform_recovery(f"{task} attempt {attempt} failed")
                    recoveries = state["recoveries"]
                    if isinstance(recoveries, list):
                        recoveries.append(recovery)
                    if not _bool(recovery.get("successful")):
                        recovery_blocked = True
                        break

                if global_timed_out[0] or recovery_blocked:
                    for remaining in plan[row_index + 1 :]:
                        remaining_state = states[str(remaining["task"])]
                        remaining_state["status"] = "skipped"
                        remaining_state["why"] = (
                            "overall timeout" if global_timed_out[0] else "recovery could not restore the home page"
                        )
                    break

    except Exception as exc:
        runner_error = f"{type(exc).__name__}: {exc}"
        if core is not None and handle and core.AsstRunning(handle):
            core.AsstStop(handle)
    finally:
        elapsed = time.monotonic() - overall_started
        if core is not None and handle:
            try:
                final = _capture(core, handle)
                if final:
                    image_writer.write("final.png", final, force=True)
            except Exception:
                pass
            core.AsstDestroy(handle)
        event_file.close()
        if dll_search is not None:
            dll_search.close()
        if keep_awake_enabled:
            set_device_stay_awake(adb_path, args.address, False)

    completed = [task for task, state in states.items() if state["status"] == "completed"]
    failed = [task for task, state in states.items() if state["status"] == "failed"]
    missing = [task for task, state in states.items() if state["status"] not in {"completed", "failed"}]
    successful = (
        len(completed) == len(states)
        and not failed
        and not missing
        and not global_timed_out[0]
        and not bootstrap_failed
        and not runner_error
    )
    result = {
        "schema_version": 2,
        "successful": successful,
        "all_tasks_completed": successful,
        "completed_tasks": completed,
        "failed_tasks": failed,
        "missing_tasks": missing,
        "task_states": list(states.values()),
        "recovery_runs": recovery_runs,
        "recovery_blocked": recovery_blocked,
        "bootstrap_failed": bootstrap_failed,
        "timed_out": global_timed_out[0],
        "runner_error": runner_error or None,
        "evidence_images": image_writer.state(),
        "qwen": {
            "enabled": advisor is not None,
            "disabled_reason": advisor.disabled_reason if advisor is not None else None,
            "recovery_decisions_only": True,
            "allowed_actions": ["restart", "stop"],
            "can_control_game": False,
        },
        "elapsed_seconds": round(elapsed, 1),
        "finished_at": datetime.now().astimezone().isoformat(),
    }
    write_json_atomic(run_dir / "result.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if successful else 2

if __name__ == "__main__":
    raise SystemExit(main())
