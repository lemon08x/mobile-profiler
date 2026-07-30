"""Lifecycle adapter for the patched MaaAssistantArknights physical-device runtime."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from mobile_profiler.maa_roguelike_runner import (
    ROGUELIKE_STRATEGY_PRESETS,
    resolve_roguelike_params,
)


MAA_ARKNIGHTS_REPOSITORY = (
    "https://github.com/MaaAssistantArknights/MaaAssistantArknights"
)
MAA_ARKNIGHTS_TASKS: dict[str, dict[str, object]] = {
    "Daily": {
        "label": "每日任务队列",
        "risk": "account_mutation",
        "runner": "daily",
    },
    "Depot": {
        "label": "仓库识别（只读）",
        "risk": "read_only",
        "runner": "feature",
    },
    "OperBox": {
        "label": "干员箱识别（只读）",
        "risk": "read_only",
        "runner": "feature",
    },
    "StartUp": {
        "label": "启动并进入游戏",
        "risk": "device_state",
        "runner": "feature",
    },
    "Award": {
        "label": "领取日常 / 周常任务奖励",
        "risk": "account_mutation",
        "runner": "feature",
    },
    "Roguelike": {
        "label": "界园肉鸽单轮（受保护）",
        "risk": "gameplay",
        "runner": "roguelike",
    },
}
ARKNIGHTS_PACKAGES = (
    "com.hypergryph.arknights",
    "com.hypergryph.arknights.bilibili",
    "com.yostaren.arknights",
    "com.yostarjp.arknights",
    "com.yostarkr.arknights",
    "tw.txwy.and.arknights",
)
MAA_PACKAGED_RUNNERS = {
    "smoke": Path(__file__).with_name("maa_viewport_smoke.py"),
    "feature": Path(__file__).with_name("maa_feature_runner.py"),
    "daily": Path(__file__).with_name("maa_daily_runner.py"),
    "roguelike": Path(__file__).with_name("maa_roguelike_runner.py"),
}
MAA_GUARD_POLICY = Path(__file__).with_name("maa_guard_policy.json")

MAA_CLIENT_TYPES = ("Official", "Bilibili", "YoStarEN", "YoStarJP", "YoStarKR", "txwy")
MAA_DAILY_TASK_IDS = ("StartUp", "Fight", "Infrast", "Recruit", "Mall", "Award")
MAA_INFRAST_FACILITIES = (
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
MAA_ROGUELIKE_THEMES = ("Phantom", "Mizuki", "Sami", "Sarkaz", "JieGarden")
MAA_OPTION_DEFAULTS: dict[str, object] = {
    "client_type": "Official",
    "daily_startup": True,
    "daily_fight": True,
    "daily_infrast": True,
    "daily_recruit": True,
    "daily_mall": True,
    "daily_award": True,
    "fight_stage": "",
    "fight_medicine": 0,
    "fight_stone": 0,
    "fight_times": 0,
    "infrast_facilities": "Mfg,Trade,Control,Power,Reception,Office,Dorm",
    "infrast_drones": "Money",
    "recruit_times": 4,
    "recruit_refresh": True,
    "recruit_expedite": False,
    "mall_visit_friends": True,
    "mall_shopping": True,
    "mall_buy_first": "招聘许可",
    "mall_blacklist": "碳;家具零件;加急许可",
    "award_mail": False,
    "award_recruit": False,
    "award_orundum": False,
    "award_mining": False,
    "award_specialaccess": False,
    "daily_task_retries": 1,
    "daily_recovery_retries": 2,
    "qwen_enabled": True,
    "qwen_url": "http://192.168.31.237:8000",
    "qwen_model": "qwen3.6-27b",
    "roguelike_strategy_preset": "stable",
    "roguelike_theme": "JieGarden",
    "roguelike_mode": 0,
    "roguelike_squad": "",
    "roguelike_roles": "",
    "roguelike_core_char": "",
    "roguelike_difficulty": 0,
    "roguelike_investment_enabled": True,
    "roguelike_investments_count": 999,
    "roguelike_stop_when_investment_full": False,
    "roguelike_stop_at_final_boss": False,
    "roguelike_use_support": False,
    "roguelike_use_nonfriend_support": False,
}
MAA_INTEGER_LIMITS: dict[str, tuple[int, int]] = {
    "fight_medicine": (0, 999),
    "fight_stone": (0, 999),
    "fight_times": (0, 10_000),
    "recruit_times": (0, 100),
    "daily_task_retries": (0, 10),
    "daily_recovery_retries": (0, 10),
    "roguelike_mode": (0, 5),
    "roguelike_difficulty": (0, 100),
    "roguelike_investments_count": (0, 9999),
}


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _resolved_path(value: object) -> Path:
    return Path(str(value or "")).expanduser().resolve()


class MaaArknightsRuntimeController:
    """Run allowlisted MAA tasks and retain local, auditable evidence."""

    def __init__(
        self,
        adb: str,
        output_root: Path,
        source_root: Optional[Path] = None,
        *,
        run_func: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        platform_name: Optional[str] = None,
    ) -> None:
        self.adb = str(adb or "adb")
        self.output_root = (
            output_root / "open-source-automation" / "maa-arknights"
        ).resolve()
        self.source_root = (source_root or self._discover_source_root()).resolve()
        self._run_func = run_func
        self._popen_factory = popen_factory
        self._platform_name = platform_name or os.name
        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._log_handle = None
        self._status = "not_installed"
        self._running = False
        self._device = ""
        self._last_error = ""
        self._last_preflight: Optional[dict[str, object]] = None
        self._last_result: Optional[dict[str, object]] = None
        self._last_run_dir = ""
        self._last_log_path = ""
        self._last_exit_code: Optional[int] = None
        self._started_at: Optional[float] = None
        self._completed_at: Optional[float] = None
        self._logs: deque[dict[str, object]] = deque(maxlen=30)
        self._core_root, self._runtime_root = self._default_runtime_paths()
        self._task = "Depot"
        self._time_limit = 600
        self._option_values = dict(MAA_OPTION_DEFAULTS)
        self._allow_account_mutation = False
        self._load_config()
        error = self._install_error(self._core_root, self._runtime_root)
        self._status = "not_installed" if error else "installed"
        self._last_error = error

    @staticmethod
    def _discover_source_root() -> Path:
        package_path = Path(__file__).resolve()
        for candidate in package_path.parents:
            if (
                (candidate / "pyproject.toml").is_file()
                and (candidate / "src" / "mobile_profiler").is_dir()
            ):
                return candidate
        return package_path.parent

    def _default_runtime_paths(self) -> tuple[Path, Path]:
        core_candidates: list[Path] = []
        configured_core = os.environ.get("MOBILE_PROFILER_MAA_CORE_ROOT", "").strip()
        if configured_core:
            core_candidates.append(Path(configured_core))
        core_candidates.append(
            self.source_root
            / ".codex-research"
            / "MAA-v6.14.2-git"
            / "build-viewport"
            / "bin"
        )
        runtime_candidates: list[Path] = []
        configured_runtime = os.environ.get(
            "MOBILE_PROFILER_MAA_RUNTIME_ROOT", ""
        ).strip()
        if configured_runtime:
            runtime_candidates.append(Path(configured_runtime))
        runtime_candidates.extend(
            [
                Path.home() / "Downloads" / "MAA-v6.14.2-win-x64",
                self.output_root.parents[1]
                / "open-source-runtimes"
                / "MaaAssistantArknights",
            ]
        )
        downloads = Path.home() / "Downloads"
        if downloads.is_dir():
            runtime_candidates.extend(
                sorted(downloads.glob("MAA-v*-win-x64"), reverse=True)
            )

        def first(candidates: list[Path], marker: Path) -> Path:
            for candidate in candidates:
                if (candidate.expanduser() / marker).exists():
                    return candidate.expanduser().resolve()
            for candidate in candidates:
                return candidate.expanduser().resolve()
            return Path()

        return (
            first(core_candidates, Path("MaaCore.dll")),
            first(runtime_candidates, Path("resource")),
        )

    @property
    def _config_path(self) -> Path:
        return self.output_root / "config.json"

    def _load_config(self) -> None:
        try:
            value = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(value, dict):
            return
        core_root = str(value.get("core_root") or "").strip()
        runtime_root = str(value.get("runtime_root") or "").strip()
        task = str(value.get("task") or "").strip()
        time_limit = value.get("time_limit")
        if core_root:
            self._core_root = _resolved_path(core_root)
        if runtime_root:
            self._runtime_root = _resolved_path(runtime_root)
        if task in MAA_ARKNIGHTS_TASKS:
            self._task = task
        if isinstance(time_limit, int) and 30 <= time_limit <= 10_800:
            self._time_limit = time_limit
        stored_options = value.get("options")
        if isinstance(stored_options, dict):
            try:
                self._option_values = self._validated_option_values(stored_options)
            except ValueError:
                pass

    def _persist_config(self) -> None:
        # Account-mutation consent is intentionally process-local and never persisted.
        payload = {
            "schema_version": 1,
            "core_root": str(self._core_root),
            "runtime_root": str(self._runtime_root),
            "task": self._task,
            "time_limit": self._time_limit,
            "options": dict(self._option_values),
            "viewport": "adaptive",
            "saved_at": time.time(),
        }
        _atomic_write(
            self._config_path,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def _log(self, status: str, message: str) -> None:
        with self._lock:
            self._logs.append(
                {"time": time.time(), "status": status, "message": message}
            )

    def _adb_path(self) -> Path:
        direct = Path(self.adb).expanduser()
        if direct.is_file():
            return direct.resolve()
        discovered = shutil.which(self.adb)
        return Path(discovered).resolve() if discovered else direct.resolve()

    def _install_error(self, core_root: Path, runtime_root: Path) -> str:
        missing: list[str] = []
        if not (core_root / "MaaCore.dll").is_file():
            missing.append(f"MaaCore.dll：{core_root}")
        if not (runtime_root / "resource").is_dir():
            missing.append(f"resource：{runtime_root}")
        if not self._adb_path().is_file():
            missing.append(f"adb：{self.adb}")
        for name, path in MAA_PACKAGED_RUNNERS.items():
            if not path.is_file():
                missing.append(f"内置 {name} runner：{path}")
        if not MAA_GUARD_POLICY.is_file():
            missing.append(f"内置危险动作策略：{MAA_GUARD_POLICY}")
        if self._platform_name != "nt":
            missing.append("MaaCore 真机适配当前仅支持 Windows")
        return "；".join(missing)

    @staticmethod
    def _parse_time_limit(value: object) -> int:
        try:
            parsed = int(str(value or "600").strip())
        except ValueError as exc:
            raise ValueError("MAA time_limit must be an integer") from exc
        if parsed < 30 or parsed > 10_800:
            raise ValueError("MAA time_limit must be within 30..10800 seconds")
        return parsed

    def _validated_option_values(
        self,
        payload: dict[str, object],
    ) -> dict[str, object]:
        values = dict(self._option_values)
        for key, default in MAA_OPTION_DEFAULTS.items():
            if key not in payload:
                continue
            raw = payload[key]
            if isinstance(default, bool):
                values[key] = _bool(raw, bool(default))
                continue
            if isinstance(default, int):
                try:
                    parsed = int(str(raw).strip())
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"MAA {key} must be an integer") from exc
                minimum, maximum = MAA_INTEGER_LIMITS[key]
                if parsed < minimum or parsed > maximum:
                    raise ValueError(
                        f"MAA {key} must be within {minimum}..{maximum}"
                    )
                values[key] = parsed
                continue
            text = str(raw or "").strip()
            if len(text) > 1000:
                raise ValueError(f"MAA {key} is too long")
            values[key] = text

        client_type = str(values["client_type"])
        if client_type not in MAA_CLIENT_TYPES:
            raise ValueError(f"unsupported MAA client_type: {client_type}")
        theme = str(values["roguelike_theme"])
        if theme not in MAA_ROGUELIKE_THEMES:
            raise ValueError(f"unsupported MAA roguelike theme: {theme}")
        strategy_preset = str(values["roguelike_strategy_preset"])
        if strategy_preset not in ROGUELIKE_STRATEGY_PRESETS:
            raise ValueError(f"unsupported MAA roguelike strategy preset: {strategy_preset}")
        facilities = [
            item.strip()
            for item in str(values["infrast_facilities"]).replace("；", ",").split(",")
            if item.strip()
        ]
        invalid_facilities = [
            item for item in facilities if item not in MAA_INFRAST_FACILITIES
        ]
        if invalid_facilities:
            raise ValueError(
                "unsupported MAA infrastructure facilities: "
                + ", ".join(invalid_facilities)
            )
        values["infrast_facilities"] = ",".join(dict.fromkeys(facilities))
        if str(values["infrast_drones"]) not in {
            "_NotUse",
            "Money",
            "SyntheticJade",
            "CombatRecord",
            "PureGold",
            "OriginStone",
            "Chip",
        }:
            raise ValueError("unsupported MAA infrastructure drone target")
        qwen_url = str(values["qwen_url"])
        if qwen_url and not qwen_url.startswith(("http://", "https://")):
            raise ValueError("MAA qwen_url must be an HTTP(S) URL")
        return values

    @staticmethod
    def _daily_task_names(options: dict[str, object]) -> list[str]:
        return [
            task
            for task in MAA_DAILY_TASK_IDS
            if bool(options[f"daily_{task.lower()}"])
        ]

    @staticmethod
    def _split_items(value: object) -> list[str]:
        return [
            item.strip()
            for item in str(value or "").replace("；", ";").split(";")
            if item.strip()
        ]

    def _daily_task_options(
        self,
        options: dict[str, object],
    ) -> dict[str, dict[str, object]]:
        fight_times = int(options["fight_times"])
        return {
            "StartUp": {
                "client_type": str(options["client_type"]),
                "start_game_enabled": True,
            },
            "Fight": {
                "stage": str(options["fight_stage"]),
                "medicine": int(options["fight_medicine"]),
                "stone": int(options["fight_stone"]),
                "times": fight_times if fight_times > 0 else 2_147_483_647,
                "client_type": str(options["client_type"]),
            },
            "Infrast": {
                "facility": [
                    item
                    for item in str(options["infrast_facilities"]).split(",")
                    if item
                ],
                "drones": str(options["infrast_drones"]),
            },
            "Recruit": {
                "times": int(options["recruit_times"]),
                "refresh": bool(options["recruit_refresh"]),
                "force_refresh": bool(options["recruit_refresh"]),
                "expedite": bool(options["recruit_expedite"]),
            },
            "Mall": {
                "visit_friends": bool(options["mall_visit_friends"]),
                "shopping": bool(options["mall_shopping"]),
                "buy_first": self._split_items(options["mall_buy_first"]),
                "blacklist": self._split_items(options["mall_blacklist"]),
            },
            "Award": {
                "award": True,
                "mail": bool(options["award_mail"]),
                "recruit": bool(options["award_recruit"]),
                "orundum": bool(options["award_orundum"]),
                "mining": bool(options["award_mining"]),
                "specialaccess": bool(options["award_specialaccess"]),
            },
        }

    @staticmethod
    def _roguelike_params(options: dict[str, object]) -> dict[str, object]:
        params: dict[str, object] = {
            "theme": str(options["roguelike_theme"]),
            "mode": int(options["roguelike_mode"]),
            "starts_count": 1,
            "difficulty": (
                int(options["roguelike_difficulty"])
                if int(options["roguelike_difficulty"]) > 0
                else 2_147_483_647
            ),
            "investment_enabled": bool(options["roguelike_investment_enabled"]),
            "investments_count": int(options["roguelike_investments_count"]),
            "stop_when_investment_full": bool(
                options["roguelike_stop_when_investment_full"]
            ),
            "stop_at_final_boss": bool(options["roguelike_stop_at_final_boss"]),
            "use_support": bool(options["roguelike_use_support"]),
            "use_nonfriend_support": bool(options["roguelike_use_nonfriend_support"]),
        }
        for source, target in (
            ("roguelike_squad", "squad"),
            ("roguelike_roles", "roles"),
            ("roguelike_core_char", "core_char"),
        ):
            value = str(options[source]).strip()
            if value:
                params[target] = value
        return resolve_roguelike_params(
            str(options["roguelike_theme"]),
            params,
            strategy_preset=str(options["roguelike_strategy_preset"]),
        )

    def _mutation_requested(self, configuration: dict[str, object]) -> bool:
        task = str(configuration["task"])
        if task == "Award":
            return True
        if task != "Daily":
            return False
        options = configuration["options"]
        assert isinstance(options, dict)
        return any(
            name != "StartUp" for name in self._daily_task_names(options)
        )

    def _apply_configuration(self, configuration: dict[str, object]) -> None:
        core_root = configuration["core_root"]
        runtime_root = configuration["runtime_root"]
        options = configuration["options"]
        assert isinstance(core_root, Path) and isinstance(runtime_root, Path)
        assert isinstance(options, dict)
        self._core_root = core_root
        self._runtime_root = runtime_root
        self._task = str(configuration["task"])
        self._time_limit = int(configuration["time_limit"])
        self._option_values = dict(options)

    def _configuration(self, payload: dict[str, object]) -> dict[str, object]:
        core_value = str(payload.get("core_root") or self._core_root).strip()
        runtime_value = str(payload.get("runtime_root") or self._runtime_root).strip()
        task = str(payload.get("task") or self._task).strip()
        if not core_value or not runtime_value:
            raise ValueError("MAA core_root and runtime_root are required")
        if task not in MAA_ARKNIGHTS_TASKS:
            raise ValueError(f"unsupported MAA task: {task}")
        option_payload = {
            key: payload[key]
            for key in MAA_OPTION_DEFAULTS
            if key in payload
        }
        options = self._validated_option_values(option_payload)
        if task == "Daily" and not self._daily_task_names(options):
            raise ValueError("MAA daily queue must enable at least one task")
        return {
            "core_root": _resolved_path(core_value),
            "runtime_root": _resolved_path(runtime_value),
            "task": task,
            "time_limit": self._parse_time_limit(
                payload.get("time_limit", self._time_limit)
            ),
            "allow_account_mutation": _bool(
                payload.get("allow_account_mutation"),
                self._allow_account_mutation,
            ),
            "options": options,
        }

    def configure(self, payload: dict[str, object]) -> dict[str, object]:
        configuration = self._configuration(payload)
        with self._lock:
            if self._running:
                raise RuntimeError("MAA automation is already running")
            self._apply_configuration(configuration)
            self._allow_account_mutation = False
            self._last_preflight = None
            self._status = (
                "installed"
                if not self._install_error(self._core_root, self._runtime_root)
                else "not_installed"
            )
            self._last_error = ""
        self._persist_config()
        self._log("configured", f"已保存 MAA {self._task} 参数")
        return self.snapshot()

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        source_root = self.source_root / "src"
        source = str(source_root) if source_root.is_dir() else ""
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (source, existing) if value
        )
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        return environment

    @staticmethod
    def _json_from_output(payload: bytes) -> dict[str, object]:
        text = payload.decode("utf-8", errors="replace").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            raise RuntimeError("MAA viewport smoke did not return JSON")
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise RuntimeError("MAA viewport smoke returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("MAA viewport smoke summary must be an object")
        return value

    def _run_checked(self, command: list[str], timeout: float) -> bytes:
        result = self._run_func(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            cwd=str(self.source_root),
            env=self._environment(),
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
        output = bytes(result.stdout or b"")
        if result.returncode != 0:
            detail = output.decode("utf-8", errors="replace")[-3000:].strip()
            raise RuntimeError(detail or f"command exited with {result.returncode}")
        return output

    def _foreground_package(self, device: str) -> str:
        command = [
            str(self._adb_path()),
            "-s",
            device,
            "shell",
            "dumpsys",
            "window",
            "windows",
        ]
        try:
            output = self._run_checked(command, 20).decode(
                "utf-8", errors="replace"
            )
        except Exception:
            return ""
        lowered = output.lower()
        return next(
            (package for package in ARKNIGHTS_PACKAGES if package.lower() in lowered),
            "",
        )

    def _screen_awake(self, device: str) -> bool:
        command = [
            str(self._adb_path()),
            "-s",
            device,
            "shell",
            "dumpsys",
            "power",
        ]
        try:
            output = self._run_checked(command, 20).decode(
                "utf-8", errors="replace"
            )
        except Exception:
            return False
        normalized = " ".join(output.lower().split())
        return any(
            token in normalized
            for token in (
                "mwakefulness=awake",
                "wakefulness: awake",
                "display power: state=on",
            )
        )

    def preflight(self, payload: dict[str, object]) -> dict[str, object]:
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("MAA preflight requires device")
        configuration = self._configuration(payload)
        core_root = configuration["core_root"]
        runtime_root = configuration["runtime_root"]
        task = str(configuration["task"])
        assert isinstance(core_root, Path) and isinstance(runtime_root, Path)
        allow_mutation = bool(configuration["allow_account_mutation"])
        if self._mutation_requested(configuration) and not allow_mutation:
            raise ValueError(
                "selected MAA tasks change account state; explicitly enable account-mutation consent"
            )
        with self._lock:
            if self._running:
                raise RuntimeError("MAA automation is already running")
            self._apply_configuration(configuration)
            self._allow_account_mutation = allow_mutation
            self._device = device
            self._status = "preflighting"
            self._last_error = ""
        error = self._install_error(core_root, runtime_root)
        if error:
            with self._lock:
                self._status = "not_installed"
                self._last_error = error
            self._log("error", error)
            raise RuntimeError(error)

        self._log("preflighting", f"开始验证 {device} 的 adaptive MaaCore 与 {task}")
        preflight_root = self.output_root / "preflight"
        preflight_root.mkdir(parents=True, exist_ok=True)
        image_path = preflight_root / "viewport.png"
        command = [
            sys.executable,
            str(MAA_PACKAGED_RUNNERS["smoke"]),
            "--core-root",
            str(core_root),
            "--runtime-root",
            str(runtime_root),
            "--adb",
            str(self._adb_path()),
            "--address",
            device,
            "--viewport",
            "adaptive",
            "--output",
            str(image_path),
        ]
        try:
            smoke = self._json_from_output(self._run_checked(command, 180))
            foreground_package = self._foreground_package(device)
            screen_awake = self._screen_awake(device)
            resolution = smoke.get("resolution_event")
            resolution_outer = resolution if isinstance(resolution, dict) else {}
            resolution_details = resolution_outer.get("details")
            geometry = resolution_details if isinstance(resolution_details, dict) else {}
            width = int(geometry.get("width") or 0)
            height = int(geometry.get("height") or 0)
            landscape = width > height > 0
            foreground_ready = bool(foreground_package)
            options = configuration["options"]
            assert isinstance(options, dict)
            launch_task = task == "StartUp" or (
                task == "Daily" and bool(options["daily_startup"])
            )
            game_ready = screen_awake and landscape and (foreground_ready or launch_task)
            if not screen_awake:
                screen_state = "device_asleep"
            elif not landscape:
                screen_state = "wrong_orientation"
            elif foreground_ready:
                screen_state = "in_game"
            elif launch_task:
                screen_state = "start_screen"
            else:
                screen_state = "wrong_app"
            summary: dict[str, object] = {
                "adapter": "maa-arknights",
                "device": {"serial": device, "state": "device"},
                "screen": {
                    "screen_state": screen_state,
                    "game_ready": game_ready,
                    "foreground_package": foreground_package,
                    "screen_awake": screen_awake,
                    "width": width,
                    "height": height,
                    "logical_size": smoke.get("image_size"),
                    "viewport": geometry.get("viewport"),
                    "evidence": str(image_path),
                },
                "runtime": {
                    "core_root": str(core_root),
                    "runtime_root": str(runtime_root),
                    "task": task,
                    "time_limit": self._time_limit,
                    "options": options,
                    "viewport": "adaptive",
                },
                "policy": {
                    "risk": MAA_ARKNIGHTS_TASKS[task]["risk"],
                    "account_mutation_confirmed": allow_mutation,
                    "destructive_roguelike_fallback_guarded": task == "Roguelike",
                },
            }
        except Exception as exc:
            with self._lock:
                self._status = "error"
                self._last_error = str(exc)
            self._log("error", str(exc))
            raise RuntimeError(f"MAA preflight failed: {exc}") from exc

        self._persist_config()
        _atomic_write(
            preflight_root / "preflight.json",
            json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        with self._lock:
            self._last_preflight = summary
            self._status = "ready" if game_ready else "waiting_for_game"
            self._last_error = ""
        self._log(
            self._status,
            "MAA adaptive 预检通过" if game_ready else f"等待游戏就绪：{screen_state}",
        )
        return self.snapshot()

    def _runner_command(
        self,
        device: str,
        run_dir: Path,
        configuration: dict[str, object],
    ) -> list[str]:
        core_root = configuration["core_root"]
        runtime_root = configuration["runtime_root"]
        task = str(configuration["task"])
        time_limit = int(configuration["time_limit"])
        options = configuration["options"]
        assert isinstance(core_root, Path) and isinstance(runtime_root, Path)
        assert isinstance(options, dict)
        common = [
            "--core-root",
            str(core_root),
            "--runtime-root",
            str(runtime_root),
            "--adb",
            str(self._adb_path()),
            "--address",
            device,
            "--run-dir",
            str(run_dir),
            "--viewport",
            "adaptive",
            "--max-seconds",
            str(time_limit),
        ]
        if task == "Roguelike":
            command = [
                sys.executable,
                str(MAA_PACKAGED_RUNNERS["roguelike"]),
                *common,
                "--theme",
                str(options["roguelike_theme"]),
                "--client-type",
                str(options["client_type"]),
                "--strategy-preset",
                str(options["roguelike_strategy_preset"]),
                "--params-json",
                json.dumps(
                    self._roguelike_params(options),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "--policy",
                str(MAA_GUARD_POLICY),
            ]
            maa_source_root = core_root.parents[1] if len(core_root.parents) > 1 else None
            if maa_source_root and (maa_source_root / "resource").is_dir():
                command.extend(["--maa-source-root", str(maa_source_root)])
            return command

        if task == "Daily":
            selected_tasks = self._daily_task_names(options)
            command = [
                sys.executable,
                str(MAA_PACKAGED_RUNNERS["daily"]),
                *common,
                "--tasks",
                ",".join(selected_tasks),
                "--task-options",
                json.dumps(
                    self._daily_task_options(options),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "--task-retries",
                str(options["daily_task_retries"]),
                "--recovery-retries",
                str(options["daily_recovery_retries"]),
                "--qwen-url",
                str(options["qwen_url"]),
                "--qwen-model",
                str(options["qwen_model"]),
            ]
            if not bool(options["qwen_enabled"]):
                command.append("--no-qwen")
            if self._mutation_requested(configuration):
                if not bool(configuration["allow_account_mutation"]):
                    raise ValueError("Daily tasks require account-mutation consent")
                command.append("--allow-account-mutation")
            return command

        command = [
            sys.executable,
            str(MAA_PACKAGED_RUNNERS["feature"]),
            *common,
            "--task",
            task,
        ]
        if task == "StartUp":
            preflight = self._last_preflight or {}
            screen = preflight.get("screen")
            screen_info = screen if isinstance(screen, dict) else {}
            start_game_enabled = not bool(screen_info.get("foreground_package"))
            command.extend(
                [
                    "--params",
                    json.dumps(
                        {
                            "enable": True,
                            "client_type": "Official",
                            "start_game_enabled": start_game_enabled,
                        },
                        separators=(",", ":"),
                    ),
                ]
            )
        elif task == "Award":
            if not bool(configuration["allow_account_mutation"]):
                raise ValueError("Award requires account-mutation consent")
            command.append("--allow-account-mutation")
        return command

    def _watch_process(
        self,
        process: subprocess.Popen[bytes],
        log_handle,
        run_dir: Path,
    ) -> None:
        exit_code = process.wait()
        try:
            log_handle.flush()
            log_handle.close()
        except OSError:
            pass
        result: Optional[dict[str, object]] = None
        try:
            value = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
            result = value if isinstance(value, dict) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            result = None
        with self._lock:
            if self._process is process:
                self._process = None
                self._log_handle = None
                self._running = False
                self._last_exit_code = int(exit_code)
                self._last_result = result
                self._completed_at = time.time()
                self._allow_account_mutation = False
                if self._status == "stopping":
                    self._status = "stopped"
                elif exit_code == 0:
                    self._status = "completed"
                else:
                    self._status = "error"
                    self._last_error = (
                        str(result.get("runner_error") or "")
                        if isinstance(result, dict)
                        else ""
                    ) or f"MAA runner exited with code {exit_code}"
        self._log(self._status, self._last_error or "MAA 自动化任务已结束")

    def start(self, payload: dict[str, object]) -> dict[str, object]:
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("MAA run requires device")
        configuration = self._configuration(payload)
        with self._lock:
            if self._running:
                raise RuntimeError("MAA automation is already running")
            preflight = self._last_preflight or {}
            checked_device = preflight.get("device")
            checked_runtime = preflight.get("runtime")
            screen = preflight.get("screen")
            if not isinstance(checked_device, dict) or not isinstance(
                checked_runtime, dict
            ) or not isinstance(screen, dict):
                raise RuntimeError("run a successful MAA preflight before starting")
            expected = {
                "core_root": str(configuration["core_root"]),
                "runtime_root": str(configuration["runtime_root"]),
                "task": configuration["task"],
                "time_limit": configuration["time_limit"],
                "options": configuration["options"],
                "viewport": "adaptive",
            }
            if (
                checked_device.get("serial") != device
                or screen.get("game_ready") is not True
                or any(checked_runtime.get(key) != value for key, value in expected.items())
            ):
                raise RuntimeError(
                    "runtime options changed; rerun a successful MAA preflight"
                )
            if self._mutation_requested(configuration) and not bool(
                configuration["allow_account_mutation"]
            ):
                raise RuntimeError(
                    "selected MAA tasks require fresh account-mutation consent"
                )

            run_name = time.strftime("%Y%m%d-%H%M%S")
            run_dir = self.output_root / "runs" / f"{run_name}-{configuration['task']}"
            log_path = self.output_root / "logs" / f"{run_name}-{configuration['task']}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_path.open("wb")
            command = self._runner_command(device, run_dir, configuration)
            creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            creationflags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            try:
                process = self._popen_factory(
                    command,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.source_root),
                    env=self._environment(),
                    creationflags=creationflags,
                )
            except Exception:
                log_handle.close()
                raise
            self._process = process
            self._log_handle = log_handle
            self._running = True
            self._status = "running"
            self._device = device
            self._task = str(configuration["task"])
            self._last_run_dir = str(run_dir)
            self._last_log_path = str(log_path)
            self._last_exit_code = None
            self._last_result = None
            self._last_error = ""
            self._started_at = time.time()
            self._completed_at = None
        self._log("running", f"已在 {device} 启动 MAA {self._task}")
        watcher = threading.Thread(
            target=self._watch_process,
            args=(process, log_handle, run_dir),
            daemon=True,
            name="maa-arknights-watcher",
        )
        watcher.start()
        return self.snapshot()

    def stop(self) -> dict[str, object]:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                self._running = False
                self._allow_account_mutation = False
                if self._status == "running":
                    self._status = "stopped"
                return self.snapshot()
            self._status = "stopping"
        self._log("stopping", "正在停止 MAA runner 并释放 MaaCore 连接")
        try:
            if self._platform_name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()
            process.wait(timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        return self.snapshot()

    def _runtime_output(self) -> dict[str, object]:
        path = Path(self._last_log_path) if self._last_log_path else None
        if path is None or not path.is_file():
            return {"path": self._last_log_path, "lines": []}
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        return {"path": str(path), "lines": lines[-20:]}

    def _runtime_options(self) -> list[dict[str, object]]:
        values = self._option_values

        def select(
            option_id: str,
            label: str,
            description: str,
            value: object,
            choices: list[tuple[object, str]],
            *,
            group: str,
            scope: str = "task",
            visible_when: Optional[dict[str, object]] = None,
        ) -> dict[str, object]:
            row: dict[str, object] = {
                "id": option_id,
                "type": "select",
                "label": label,
                "description": description,
                "value": str(value),
                "options": [
                    {"value": str(choice), "label": choice_label}
                    for choice, choice_label in choices
                ],
                "group": group,
                "scope": scope,
            }
            if visible_when:
                row["visible_when"] = visible_when
            return row

        def field(
            option_id: str,
            label: str,
            description: str,
            *,
            group: str,
            input_type: str = "text",
            scope: str = "task",
            visible_when: Optional[dict[str, object]] = None,
            required: bool = False,
            minimum: Optional[int] = None,
            maximum: Optional[int] = None,
        ) -> dict[str, object]:
            value = (
                self._core_root
                if option_id == "core_root"
                else self._runtime_root
                if option_id == "runtime_root"
                else values.get(option_id, "")
            )
            row: dict[str, object] = {
                "id": option_id,
                "type": input_type,
                "label": label,
                "description": description,
                "value": str(value),
                "group": group,
                "scope": scope,
                "required": required,
            }
            if visible_when:
                row["visible_when"] = visible_when
            if minimum is not None:
                row["min"] = minimum
            if maximum is not None:
                row["max"] = maximum
            return row

        def checkbox(
            option_id: str,
            label: str,
            description: str,
            *,
            group: str,
            visible_when: Optional[dict[str, object]] = None,
        ) -> dict[str, object]:
            row: dict[str, object] = {
                "id": option_id,
                "type": "checkbox",
                "label": label,
                "description": description,
                "value": bool(values.get(option_id, False)),
                "group": group,
                "scope": "task",
            }
            if option_id == "allow_account_mutation":
                row["value"] = self._allow_account_mutation
                row["persisted"] = False
            if visible_when:
                row["visible_when"] = visible_when
            return row

        daily = {"id": "task", "equals": "Daily"}
        roguelike = {"id": "task", "equals": "Roguelike"}
        mutation = {"id": "task", "in": ["Daily", "Award"]}
        options: list[dict[str, object]] = [
            select(
                "task",
                "运行模式",
                "选择完整每日队列、肉鸽或只读诊断任务。",
                self._task,
                [(task, str(info["label"])) for task, info in MAA_ARKNIGHTS_TASKS.items()],
                group="运行模式",
            ),
            select(
                "client_type",
                "客户端",
                "决定游戏包名、资源语言和商店默认文本。",
                values["client_type"],
                [(item, item) for item in MAA_CLIENT_TYPES],
                group="运行模式",
            ),
            select(
                "time_limit",
                "最长运行时间",
                "到时由 runner 停止并保留证据。",
                self._time_limit,
                [(600, "10 分钟"), (1800, "30 分钟"), (3600, "1 小时"), (10800, "3 小时")],
                group="运行限制",
            ),
        ]
        for option_id, label, description in (
            ("daily_startup", "启动游戏", "队列开始时启动并进入明日方舟。"),
            ("daily_fight", "理智作战", "按下方关卡与药剂设置执行 Fight。"),
            ("daily_infrast", "基建换班", "按设施范围执行基建收取和换班。"),
            ("daily_recruit", "公开招募", "刷新标签并按星级策略确认招募。"),
            ("daily_mall", "信用商店", "访问好友并按购物清单消耗信用。"),
            ("daily_award", "领取奖励", "领取日常/周常任务奖励。"),
        ):
            options.append(
                checkbox(
                    option_id,
                    label,
                    description,
                    group="每日任务队列",
                    visible_when=daily,
                )
            )
        options.extend(
            [
                field("fight_stage", "关卡", "留空由 MAA 选择当前可用关卡，例如 1-7、CE-6。", group="理智作战", visible_when=daily),
                field("fight_medicine", "可用理智药", "允许使用的理智药数量；0 表示不使用。", group="理智作战", input_type="number", visible_when=daily, minimum=0, maximum=999),
                field("fight_stone", "可用源石", "允许碎石次数；建议保持 0。", group="理智作战", input_type="number", visible_when=daily, minimum=0, maximum=999),
                field("fight_times", "作战次数", "0 表示持续到理智不足，否则执行指定次数。", group="理智作战", input_type="number", visible_when=daily, minimum=0, maximum=10000),
                field("infrast_facilities", "基建设施", "逗号分隔；可用 Mfg, Trade, Control, Power, Reception, Office, Dorm。", group="基建", visible_when=daily),
                select("infrast_drones", "无人机用途", "选择无人机加速目标。", values["infrast_drones"], [("_NotUse", "不使用"), ("Money", "龙门币"), ("SyntheticJade", "合成玉"), ("CombatRecord", "作战记录"), ("PureGold", "赤金"), ("OriginStone", "源石碎片"), ("Chip", "芯片")], group="基建", visible_when=daily),
                field("recruit_times", "招募次数", "本轮最多处理的公开招募槽位数。", group="公开招募", input_type="number", visible_when=daily, minimum=0, maximum=100),
                checkbox("recruit_refresh", "刷新三星标签", "允许刷新没有高星组合的标签。", group="公开招募", visible_when=daily),
                checkbox("recruit_expedite", "使用加急许可", "允许消耗加急许可立即完成招募。", group="公开招募", visible_when=daily),
                checkbox("mall_visit_friends", "访问好友", "领取好友信用。", group="信用商店", visible_when=daily),
                checkbox("mall_shopping", "自动购物", "按优先清单与黑名单购买物品。", group="信用商店", visible_when=daily),
                field("mall_buy_first", "优先购买", "使用分号分隔物品名称。", group="信用商店", visible_when=daily),
                field("mall_blacklist", "不购买", "使用分号分隔物品名称。", group="信用商店", visible_when=daily),
                checkbox("award_mail", "领取邮件", "同时领取邮件附件。", group="奖励", visible_when=daily),
                checkbox("award_recruit", "领取招募奖励", "同时领取招募相关奖励。", group="奖励", visible_when=daily),
                checkbox("award_orundum", "领取合成玉", "同时领取可用合成玉奖励。", group="奖励", visible_when=daily),
                checkbox("award_mining", "领取矿区奖励", "同时处理矿区奖励。", group="奖励", visible_when=daily),
                checkbox("award_specialaccess", "领取特别登录奖励", "同时处理特别登录活动奖励。", group="奖励", visible_when=daily),
                field("daily_task_retries", "单任务重试", "任务失败后的原地重试次数。", group="无人值守恢复", input_type="number", visible_when=daily, minimum=0, maximum=10),
                field("daily_recovery_retries", "恢复重试", "重启游戏后的恢复次数。", group="无人值守恢复", input_type="number", visible_when=daily, minimum=0, maximum=10),
                checkbox("qwen_enabled", "启用本地 Qwen 恢复判断", "模型只能在“重启 / 停止”之间决策，不能直接操作游戏。", group="无人值守恢复", visible_when=daily),
                field("qwen_url", "Qwen 服务地址", "OpenAI 兼容本地服务地址。", group="无人值守恢复", visible_when=daily),
                field("qwen_model", "Qwen 模型", "用于异常恢复判断的本地模型名。", group="无人值守恢复", visible_when=daily),
                select("roguelike_strategy_preset", "策略预设", "稳定单轮会关闭投资等非必要分支，但保留难度与账号相关编队设置；自定义则完全使用下方参数。", values["roguelike_strategy_preset"], [("stable", "稳定单轮（推荐）"), ("custom", "自定义参数")], group="肉鸽策略", visible_when=roguelike),
                select("roguelike_theme", "肉鸽主题", "选择 MAA 已安装资源支持的集成战略主题。", values["roguelike_theme"], [(item, item) for item in MAA_ROGUELIKE_THEMES], group="肉鸽策略", visible_when=roguelike),
                select("roguelike_mode", "运行模式", "0 常规刷取；1 侧重投资；2 兼顾刷取与投资。", values["roguelike_mode"], [(0, "常规刷取"), (1, "投资优先"), (2, "刷取 + 投资")], group="肉鸽策略", visible_when=roguelike),
                field("roguelike_squad", "分队", "留空使用 MAA 默认分队；填写上游资源中的分队名。", group="肉鸽编队", visible_when=roguelike),
                field("roguelike_roles", "职业组", "留空使用默认；填写上游支持的职业组名。", group="肉鸽编队", visible_when=roguelike),
                field("roguelike_core_char", "核心干员", "留空自动选择；可填写希望优先招募的干员名。", group="肉鸽编队", visible_when=roguelike),
                field("roguelike_difficulty", "难度", "0 表示使用当前/最高可用难度。", group="肉鸽策略", input_type="number", visible_when=roguelike, minimum=0, maximum=100),
                checkbox("roguelike_investment_enabled", "启用投资", "遇到投资系统时尝试存入源石锭。", group="肉鸽投资", visible_when=roguelike),
                field("roguelike_investments_count", "投资上限", "本轮最多尝试投资的次数。", group="肉鸽投资", input_type="number", visible_when=roguelike, minimum=0, maximum=9999),
                checkbox("roguelike_stop_when_investment_full", "投资满后停止", "投资系统达到上限后结束本轮。", group="肉鸽投资", visible_when=roguelike),
                checkbox("roguelike_stop_at_final_boss", "最终 Boss 前停止", "保留最终战前状态。", group="肉鸽策略", visible_when=roguelike),
                checkbox("roguelike_use_support", "使用助战", "允许在招募时选择好友助战。", group="肉鸽编队", visible_when=roguelike),
                checkbox("roguelike_use_nonfriend_support", "允许非好友助战", "助战不足时可选择非好友干员。", group="肉鸽编队", visible_when=roguelike),
                checkbox("allow_account_mutation", "确认本次账号变更", "每日队列或领奖会消耗/领取账号资源；确认不会持久化，运行结束后自动重置。", group="风险确认", visible_when=mutation),
                field("core_root", "Adaptive Core 目录", "包含已应用非 16:9 viewport 补丁的 MaaCore.dll。", group="安装路径", scope="environment", required=True),
                field("runtime_root", "MAA 发布目录", "包含官方 resource 目录的 MAA Windows 发布包。", group="安装路径", scope="environment", required=True),
            ]
        )
        return options

    def snapshot(self) -> dict[str, object]:
        install_error = self._install_error(self._core_root, self._runtime_root)
        available = not install_error
        core_path = self._core_root / "MaaCore.dll"
        disk_bytes = core_path.stat().st_size if core_path.is_file() else 0
        with self._lock:
            process = self._process
            running = self._running and process is not None and process.poll() is None
            if self._status == "not_installed" and available:
                self._status = "installed"
                self._last_error = ""
            status = "running" if running else self._status
            return {
                "adapter_id": "maa-arknights",
                "end_to_end_verified": True,
                "verification": {
                    "status": "verified",
                    "device_resolution": "2800x1260",
                    "completed_flows": [
                        "StartUp",
                        "Depot",
                        "OperBox",
                        "Award",
                        "Roguelike natural settlement",
                    ],
                    "coordinate_model": "display -> adaptive viewport -> logical",
                },
                "status": status,
                "running": running,
                "available": available,
                "device": self._device,
                "upstream": {
                    "repository": MAA_ARKNIGHTS_REPOSITORY,
                    "license": "AGPL-3.0",
                    "version": "v6.14.2 adaptive",
                    "path": str(self._core_root),
                    "runtime_path": str(self._runtime_root),
                    "disk_bytes": disk_bytes,
                    "disk_mib": round(disk_bytes / 1024 / 1024, 1),
                },
                "preflight": self._last_preflight,
                "result": self._last_result,
                "progress": {
                    "task": self._task,
                    "task_label": str(MAA_ARKNIGHTS_TASKS[self._task]["label"]),
                    "risk": str(MAA_ARKNIGHTS_TASKS[self._task]["risk"]),
                    "viewport": "adaptive",
                },
                "runtime_options": self._runtime_options(),
                "capabilities": {
                    "configure": True,
                    "configure_when_unavailable": True,
                    "packaged_runner": True,
                    "preflight": True,
                    "start": True,
                    "stop": True,
                    "screenshot": True,
                },
                "safety": {
                    "viewport": "adaptive",
                    "task_allowlist": list(MAA_ARKNIGHTS_TASKS),
                    "award_requires_account_mutation_consent": True,
                    "roguelike_destructive_fallback_guarded": True,
                    "raw_evidence_local_only": True,
                },
                "last_error": self._last_error or (install_error if not available else ""),
                "last_run_dir": self._last_run_dir,
                "last_exit_code": self._last_exit_code,
                "started_at": self._started_at,
                "completed_at": self._completed_at,
                "runtime_output": self._runtime_output(),
                "logs": list(self._logs),
            }

    def close(self) -> None:
        try:
            self.stop()
        except Exception:
            pass
