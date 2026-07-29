"""Run an external StarRailCopilot checkout against one Android device.

StarRailCopilot (SRC) already owns its Android transport: screenshots use ADB,
uiautomator2, or scrcpy and input uses MaaTouch or minitouch.  This subprocess
boundary therefore configures and invokes SRC directly; it deliberately does
not emulate desktop keyboard, mouse, or win32 APIs.

The upstream checkout remains a separate GPL-3.0 program.  Mobile Profiler
validates and launches a user-provided checkout but does not bundle its code or
assets.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence


UPSTREAM_REPOSITORY = "https://github.com/LmeSzinc/StarRailCopilot"
UPSTREAM_LICENSE = "GPL-3.0"
LOGICAL_RESOLUTION = (1280, 720)
CONFIG_NAME = "mobile-profiler"
ADAPTIVE_GEOMETRY_PATCH_VERSION = "mobile-profiler-src-0f2aaf8c-v2"
MIN_ADAPTIVE_CAPTURE_HEIGHT = 720
MAX_ADAPTIVE_ASPECT_RATIO = 3.0
SCRCPY_MAX_SIZES = (1600, 1920)
DEFAULT_SCRCPY_MAX_SIZE = 1920

SERVER_PACKAGES = {
    "CN-Official": "com.miHoYo.hkrpg",
    "CN-Bilibili": "com.miHoYo.hkrpg.bilibili",
    "OVERSEA-America": "com.HoYoverse.hkrpgoversea",
    "OVERSEA-Asia": "com.HoYoverse.hkrpgoversea",
    "OVERSEA-Europe": "com.HoYoverse.hkrpgoversea",
    "OVERSEA-TWHKMO": "com.HoYoverse.hkrpgoversea",
    "VN-Official": "com.HoYoverse.hkrpgvn",
}

SCREENSHOT_METHODS = ("scrcpy", "ADB", "uiautomator2")
CONTROL_METHODS = ("MaaTouch", "minitouch")
GAME_LANGUAGES = ("auto", "cn", "en")
ROGUE_WORLDS = (
    "Simulated_Universe_World_3",
    "Simulated_Universe_World_4",
    "Simulated_Universe_World_5",
    "Simulated_Universe_World_6",
    "Simulated_Universe_World_8",
)
ROGUE_PATHS = (
    "Preservation",
    "Remembrance",
    "Nihility",
    "Abundance",
    "The_Hunt",
    "Destruction",
    "Elation",
    "Propagation",
    "Erudition",
)
DOMAIN_STRATEGIES = ("combat", "occurrence")
WORKFLOWS = ("rogue", "daily")
DAILY_TASKS = (
    ("Dungeon", "dungeon"),
    ("Assignment", "assignment"),
    ("BattlePass", "battle_pass"),
    ("DailyQuest", "daily_quest"),
    ("Freebies", "freebies"),
    ("DataUpdate", "data_update"),
)


def _adaptive_geometry_metadata(root: Path) -> dict[str, object]:
    """Detect the complete patch without importing the GPL checkout."""

    sentinels = {
        "recognition": (
            root / "module" / "base" / "base.py",
            "def appear_then_click(self, button, interval=5, similarity=0.85,",
        ),
        "geometry": (
            root / "module" / "device" / "geometry.py",
            f'ADAPTIVE_GEOMETRY_PATCH_VERSION = "{ADAPTIVE_GEOMETRY_PATCH_VERSION}"',
        ),
        "screenshot": (
            root / "module" / "device" / "screenshot.py",
            "def capture_geometry_for(self, width, height):",
        ),
        "control": (
            root / "module" / "device" / "control.py",
            "def _logical_to_backend(self, point, alignment, method, frame_id=None):",
        ),
        "minitouch": (
            root / "module" / "device" / "method" / "minitouch.py",
            "coordinate_space='display'",
        ),
        "maatouch": (
            root / "module" / "device" / "method" / "maatouch.py",
            "coordinate_space=coordinate_space",
        ),
        "scrcpy": (
            root / "module" / "device" / "method" / "scrcpy" / "options.py",
            "SRC_SCRCPY_MAX_SIZE",
        ),
        "scrcpy_control": (
            root / "module" / "device" / "method" / "scrcpy" / "scrcpy.py",
            "def click_scrcpy(self, x, y, contact=None):",
        ),
        "uiautomator2": (
            root / "module" / "device" / "method" / "uiautomator_2.py",
            "Check that the display can contain a 1280x720 logical viewport.",
        ),
        "joystick": (
            root / "tasks" / "map" / "control" / "joystick.py",
            "alignment=Alignment.LEFT",
        ),
        "map_radar": (
            root / "tasks" / "map" / "minimap" / "radar.py",
            "self.device.image_for(Alignment.LEFT)",
        ),
        "combat_state": (
            root / "tasks" / "combat" / "state.py",
            "COMBAT_PAUSE, alignment=Alignment.RIGHT",
        ),
        "main_shortcuts_mobile": (
            root / "tasks" / "base" / "page.py",
            "_right_anchored_main_button._preferred_alignment = 'right'",
        ),
        "daily_assignment_mobile": (
            root / "tasks" / "assignment" / "state.py",
            "empty_confirm = Timer(0.8, count=2).start()",
        ),
        "daily_combat_wave_mobile": (
            root / "tasks" / "combat" / "prepare.py",
            "self.match_template_luma(WAVE_CHECK, similarity=0.80)",
        ),
        "daily_battle_pass_mobile": (
            root / "tasks" / "battle_pass" / "battle_pass.py",
            "scroll.next_page(main=self, alignment=Alignment.RIGHT)",
        ),
        "daily_support_reward_mobile": (
            root / "tasks" / "base" / "ui.py",
            "SUPPORT_REWARD_POPUP.set_search_offset((-60, -40, 180, 60))",
        ),
        "daily_item_tabs_mobile": (
            root / "tasks" / "item" / "ui.py",
            "main.match_template_luma(button, alignment='left')",
        ),
        "daily_item_data_mobile": (
            root / "tasks" / "item" / "data_update.py",
            "self.device.image_for('right')",
        ),
        "rogue_route_checkpoint": (
            root / "tasks" / "rogue" / "route" / "loader.py",
            "ROUTE_CHECKPOINT_VERSION = 1",
        ),
        "rogue_wide_item_fallback": (
            root / "tasks" / "rogue" / "route" / "base.py",
            "poor_try = self._is_wide_capture()",
        ),
    }
    present: list[str] = []
    missing: list[str] = []
    for name, (path, sentinel) in sentinels.items():
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        if sentinel in content:
            present.append(name)
        else:
            missing.append(name)
    return {
        "available": not missing,
        "version": ADAPTIVE_GEOMETRY_PATCH_VERSION if not missing else "",
        "base_commit": "0f2aaf8c86772186e93bca830c998c5ddac12758",
        "components": present,
        "missing_components": missing,
        "logical_resolution": list(LOGICAL_RESOLUTION),
        "minimum_capture_height": MIN_ADAPTIVE_CAPTURE_HEIGHT,
        "maximum_aspect_ratio": MAX_ADAPTIVE_ASPECT_RATIO,
    }


def _git_commit(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return bytes(result.stdout or b"").decode("ascii", errors="ignore").strip()


def validate_upstream_path(path: Path) -> dict[str, object]:
    """Validate the small set of SRC files required by this adapter."""

    root = path.expanduser().resolve()
    required = (
        root / "src.py",
        root / "module" / "alas.py",
        root / "module" / "device" / "screenshot.py",
        root / "tasks" / "rogue" / "rogue.py",
        root / "tasks" / "map" / "control" / "joystick.py",
        root / "config" / "template.json",
        root / "route" / "rogue" / "route.json",
        root / "LICENSE",
    )
    missing = [str(item.relative_to(root)) for item in required if not item.is_file()]
    if missing:
        raise RuntimeError(
            "StarRailCopilot checkout is incomplete: " + ", ".join(missing)
        )

    license_text = (root / "LICENSE").read_text(
        encoding="utf-8", errors="replace"
    )
    normalized_license = " ".join(license_text.upper().split())
    if (
        "GNU GENERAL PUBLIC LICENSE" not in normalized_license
        or "VERSION 3" not in normalized_license
    ):
        raise RuntimeError("StarRailCopilot checkout has an unexpected license")

    route_count = sum(
        1
        for item in (root / "route" / "rogue").rglob("*.py")
        if item.is_file() and item.name != "__init__.py"
    )
    scrcpy_versions = sorted(
        {
            match.group(1)
            for item in (root / "bin" / "scrcpy").glob("scrcpy-server-*.jar")
            if (match := re.search(r"scrcpy-server-v?(.+)\.jar$", item.name))
        }
    )
    return {
        "path": str(root),
        "repository": UPSTREAM_REPOSITORY,
        "license": UPSTREAM_LICENSE,
        "commit": _git_commit(root),
        "route_count": route_count,
        "scrcpy_server_versions": scrcpy_versions,
        "logical_resolution": list(LOGICAL_RESOLUTION),
        "adaptive_geometry": _adaptive_geometry_metadata(root),
    }


def _adaptive_resolution_supported(width: int, height: int) -> bool:
    if width <= height or height < MIN_ADAPTIVE_CAPTURE_HEIGHT:
        return False
    aspect_ratio = width / height
    logical_aspect = LOGICAL_RESOLUTION[0] / LOGICAL_RESOLUTION[1]
    return logical_aspect <= aspect_ratio <= MAX_ADAPTIVE_ASPECT_RATIO


def _project_scrcpy_capture(
    width: int, height: int, max_size: int
) -> tuple[int, int]:
    if width <= 0 or height <= 0 or width <= max_size:
        return width, height
    scale = max_size / width
    return max_size, int(round(height * scale))


def _run_adb(
    adb: str,
    serial: str,
    arguments: Sequence[str],
    *,
    timeout: float = 20,
    run_func: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> subprocess.CompletedProcess[bytes]:
    command = [str(adb or "adb"), "-s", serial, *map(str, arguments)]
    try:
        return run_func(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"ADB executable was not found: {adb}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"ADB command timed out for {serial}: {' '.join(arguments)}"
        ) from exc


def _decode(result: subprocess.CompletedProcess[bytes]) -> str:
    return bytes(result.stdout or b"").decode("utf-8", errors="replace").strip()


def _error_text(result: subprocess.CompletedProcess[bytes]) -> str:
    return bytes(result.stderr or b"").decode("utf-8", errors="replace").strip()


def _png_dimensions(payload: bytes) -> tuple[int, int]:
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("ADB screencap did not return a PNG image")
    if payload[12:16] != b"IHDR":
        raise RuntimeError("ADB screencap PNG is missing IHDR")
    width, height = struct.unpack(">II", payload[16:24])
    if width <= 0 or height <= 0:
        raise RuntimeError("ADB screencap returned invalid dimensions")
    return int(width), int(height)


def _foreground_activity(payload: str) -> tuple[str, str]:
    patterns = (
        r"(?:topResumedActivity|mResumedActivity|ResumedActivity).*?\s"
        r"([A-Za-z0-9._]+)/(\S+)",
        r"ACTIVITY\s+([A-Za-z0-9._]+)/(\S+)",
    )
    for pattern in patterns:
        match = re.search(pattern, payload)
        if match:
            return match.group(1), match.group(2).rstrip("}")
    return "", ""


def preflight_device(
    *,
    adb: str,
    serial: str,
    server: str,
    adaptive_geometry: bool = False,
    screenshot_method: str = "",
    scrcpy_max_size: int = DEFAULT_SCRCPY_MAX_SIZE,
    run_func: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> dict[str, object]:
    """Run a read-only ADB preflight without importing SRC dependencies."""

    package = SERVER_PACKAGES[server]
    state_result = _run_adb(adb, serial, ("get-state",), run_func=run_func)
    device_state = _decode(state_result)
    if state_result.returncode != 0 or device_state != "device":
        return {
            "serial": serial,
            "server": server,
            "package": package,
            "device_state": device_state or "unavailable",
            "package_installed": False,
            "foreground_package": "",
            "foreground_activity": "",
            "foreground_matches": False,
            "width": 0,
            "height": 0,
            "orientation": "unknown",
            "orientation_matches": False,
            "resolution_supported": False,
            "resolution_mode": "unsupported",
            "adaptive_geometry_available": adaptive_geometry,
            "projected_capture_width": 0,
            "projected_capture_height": 0,
            "capture_resolution_supported": False,
            "required_resolution": list(LOGICAL_RESOLUTION),
            "screen_state": "device_offline",
            "game_ready": False,
            "adb_error": _error_text(state_result),
        }

    package_result = _run_adb(
        adb,
        serial,
        ("shell", "pm", "path", package),
        run_func=run_func,
    )
    package_installed = (
        package_result.returncode == 0 and _decode(package_result).startswith("package:")
    )
    if not package_installed:
        return {
            "serial": serial,
            "server": server,
            "package": package,
            "device_state": device_state,
            "package_installed": False,
            "foreground_package": "",
            "foreground_activity": "",
            "foreground_matches": False,
            "width": 0,
            "height": 0,
            "orientation": "unknown",
            "orientation_matches": False,
            "resolution_supported": False,
            "resolution_mode": "unsupported",
            "adaptive_geometry_available": adaptive_geometry,
            "projected_capture_width": 0,
            "projected_capture_height": 0,
            "capture_resolution_supported": False,
            "required_resolution": list(LOGICAL_RESOLUTION),
            "screen_state": "package_missing",
            "game_ready": False,
            "adb_error": _error_text(package_result),
        }

    activity_result = _run_adb(
        adb,
        serial,
        ("shell", "dumpsys", "activity", "activities"),
        run_func=run_func,
    )
    foreground_package, foreground_activity = _foreground_activity(
        _decode(activity_result)
    )
    foreground_matches = foreground_package == package

    screenshot_result = _run_adb(
        adb,
        serial,
        ("exec-out", "screencap", "-p"),
        timeout=30,
        run_func=run_func,
    )
    try:
        if screenshot_result.returncode != 0:
            raise RuntimeError(_error_text(screenshot_result) or "ADB screencap failed")
        width, height = _png_dimensions(bytes(screenshot_result.stdout or b""))
        screenshot_error = ""
    except RuntimeError as exc:
        width, height = 0, 0
        screenshot_error = str(exc)

    landscape = width > height > 0
    native_resolution = (width, height) == LOGICAL_RESOLUTION
    adaptive_resolution = (
        adaptive_geometry and _adaptive_resolution_supported(width, height)
    )
    physical_resolution_supported = native_resolution or adaptive_resolution
    if screenshot_method.lower() == "scrcpy":
        projected_width, projected_height = _project_scrcpy_capture(
            width, height, scrcpy_max_size
        )
    else:
        projected_width, projected_height = width, height
    capture_resolution_supported = (
        projected_height >= MIN_ADAPTIVE_CAPTURE_HEIGHT
        if physical_resolution_supported
        else False
    )
    resolution_supported = (
        physical_resolution_supported and capture_resolution_supported
    )
    resolution_mode = (
        "native-logical"
        if native_resolution and capture_resolution_supported
        else "adaptive-viewport"
        if adaptive_resolution and capture_resolution_supported
        else "capture-too-small"
        if physical_resolution_supported
        else "unsupported"
    )
    if screenshot_error:
        screen_state = "screenshot_error"
    elif not landscape:
        screen_state = "wrong_orientation"
    elif not foreground_matches:
        screen_state = "wrong_app"
    elif not physical_resolution_supported:
        screen_state = "unsupported_resolution"
    elif not capture_resolution_supported:
        screen_state = "unsupported_capture_resolution"
    else:
        screen_state = "in_game"
    game_ready = screen_state == "in_game"

    return {
        "serial": serial,
        "server": server,
        "package": package,
        "device_state": device_state,
        "package_installed": package_installed,
        "foreground_package": foreground_package,
        "foreground_activity": foreground_activity,
        "foreground_matches": foreground_matches,
        "width": width,
        "height": height,
        "orientation": "landscape" if landscape else "portrait" if height else "unknown",
        "orientation_matches": landscape,
        "resolution_supported": resolution_supported,
        "resolution_mode": resolution_mode,
        "adaptive_geometry_available": adaptive_geometry,
        "projected_capture_width": projected_width,
        "projected_capture_height": projected_height,
        "capture_resolution_supported": capture_resolution_supported,
        "required_resolution": list(LOGICAL_RESOLUTION),
        "screen_state": screen_state,
        "game_ready": game_ready,
        "adb_error": screenshot_error or _error_text(activity_result),
    }


def _atomic_json_write(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _merge_config(
    defaults: dict[str, object],
    persisted: dict[str, object],
) -> dict[str, object]:
    """Overlay persisted values without losing new keys from the template."""

    merged: dict[str, object] = {}
    for key in defaults.keys() | persisted.keys():
        default = defaults.get(key)
        value = persisted.get(key)
        if isinstance(default, dict) and isinstance(value, dict):
            merged[key] = _merge_config(default, value)
        elif key in persisted:
            merged[key] = value
        else:
            merged[key] = default
    return merged


def build_src_config(root: Path, args: argparse.Namespace) -> Path:
    """Create or update the dedicated persistent SRC instance."""

    template_path = root / "config" / "template.json"
    try:
        template = json.loads(template_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"unable to read SRC template config: {exc}") from exc
    if not isinstance(template, dict):
        raise RuntimeError("SRC template config must be a JSON object")
    config_path = root / "config" / f"{CONFIG_NAME}.json"
    config = template
    if config_path.is_file():
        try:
            persisted = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"unable to read persisted SRC config: {exc}") from exc
        if not isinstance(persisted, dict):
            raise RuntimeError("persisted SRC config must be a JSON object")
        previous_emulator = dict(
            dict(persisted.get("Alas") or {}).get("Emulator") or {}
        )
        same_device = previous_emulator.get("Serial") in (None, "", args.serial)
        same_server = previous_emulator.get("PackageName") in (
            None,
            "",
            args.server,
        )
        if same_device and same_server:
            config = _merge_config(template, persisted)
    try:
        emulator = config["Alas"]["Emulator"]
        rogue = config["Rogue"]["RogueWorld"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("SRC template config is missing Alas/Emulator or Rogue") from exc

    emulator.update(
        {
            "Serial": args.serial,
            "GameClient": "android",
            "PackageName": args.server,
            "GameLanguage": getattr(args, "game_language", "cn"),
            "ScreenshotMethod": args.screenshot_method,
            "ControlMethod": args.control_method,
            "AdbRestart": False,
        }
    )
    rogue.update(
        {
            "World": args.world,
            "Path": args.path,
            "DomainStrategy": args.domain_strategy,
            "UseImmersifier": args.use_immersifier,
            "DoubleEvent": args.double_event,
            "WeeklyFarming": args.weekly_farming,
            "UseStamina": args.use_stamina,
        }
    )
    if getattr(args, "workflow", "rogue") == "daily":
        for task, _ in DAILY_TASKS:
            try:
                config[task]["Scheduler"]["Enable"] = True
            except (KeyError, TypeError) as exc:
                raise RuntimeError(
                    f"SRC template config is missing {task}/Scheduler"
                ) from exc
    _atomic_json_write(config_path, config)
    return config_path


def _prepend_adb_binary(adb: str) -> None:
    """Let SRC's adbutils layer use the same host ADB as the preflight."""

    if not adb or adb == "adb":
        return
    from module.device.connection_attr import ConnectionAttr

    supplied = Path(adb).expanduser()
    located = supplied if supplied.is_file() else Path(shutil.which(adb) or adb)
    candidate = str(located.resolve())
    ConnectionAttr.adb_binary_list = [
        candidate,
        *[item for item in ConnectionAttr.adb_binary_list if str(item) != candidate],
    ]


def run_src_rogue(root: Path, args: argparse.Namespace) -> bool:
    """Run exactly one Rogue cycle after the preflight accepts the device.

    ``Rogue.run()`` is an Alas scheduler entry point and may dispatch unrelated
    tasks (for example Dungeon) before touching Rogue.  The mobile-profiler
    adapter is deliberately a one-shot runner, so call the task's domain entry
    directly instead.
    """

    os.chdir(root)
    os.environ["SRC_SCRCPY_MAX_SIZE"] = str(args.scrcpy_max_size)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    _prepend_adb_binary(args.adb)

    from module.config.config import AzurLaneConfig
    from module.exception import HandledError
    from src import StarRailCopilot
    from tasks.rogue.rogue import Rogue

    config = AzurLaneConfig(CONFIG_NAME, task="Rogue")
    application = StarRailCopilot(CONFIG_NAME)
    # ``cached_property`` is intentionally seeded so Device and Rogue share the
    # same task-bound configuration rather than a second Alas-only instance.
    application.__dict__["config"] = config
    rogue = Rogue(config=config, device=application.device)
    while True:
        try:
            return bool(rogue.rogue_once())
        except HandledError:
            # SRC raises this after recovering a known global state such as
            # the login page.  Its scheduler reruns the same task; mirror that
            # behavior without dispatching any unrelated scheduled task.
            continue


def _datetime_text(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone().isoformat() if value.tzinfo else value.isoformat()
    return str(value or "")


def _task_due(config: object, command: str) -> tuple[bool, str, bool]:
    enabled = bool(config.cross_get(f"{command}.Scheduler.Enable", default=False))
    next_run = config.cross_get(f"{command}.Scheduler.NextRun", default=None)
    if not enabled:
        return False, _datetime_text(next_run), False
    if isinstance(next_run, str):
        try:
            next_run = datetime.fromisoformat(next_run)
        except ValueError:
            next_run = None
    due = not isinstance(next_run, datetime) or next_run <= datetime.now()
    return due, _datetime_text(next_run), True


def run_src_daily(root: Path, args: argparse.Namespace) -> dict[str, object]:
    """Run one bounded SRC daily queue and verify all 500-point rewards."""

    os.chdir(root)
    os.environ["SRC_SCRCPY_MAX_SIZE"] = str(args.scrcpy_max_size)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    _prepend_adb_binary(args.adb)

    from module.base.resource import release_resources
    from module.config.config import AzurLaneConfig
    from src import StarRailCopilot
    from tasks.daily.daily_quest import DailyQuestUI

    bootstrap = AzurLaneConfig(CONFIG_NAME, task="DailyQuest")
    application = StarRailCopilot(CONFIG_NAME)
    application.__dict__["config"] = bootstrap
    device = application.device
    task_results: list[dict[str, object]] = []

    for command, method in DAILY_TASKS:
        config = AzurLaneConfig(CONFIG_NAME, task=command)
        due, next_run_before, enabled = _task_due(config, command)
        if not enabled:
            task_results.append(
                {
                    "task": command,
                    "status": "failed_disabled",
                    "attempts": 0,
                    "next_run_before": next_run_before,
                }
            )
            continue
        if not due:
            task_results.append(
                {
                    "task": command,
                    "status": "skipped_not_due",
                    "attempts": 0,
                    "next_run_before": next_run_before,
                }
            )
            continue

        release_resources(next_task=command)
        application.__dict__["config"] = config
        device.config = config
        success = False
        attempts = 0
        exit_code: int | None = None
        for attempts in range(1, 4):
            try:
                success = bool(application.run(method))
            except SystemExit as exc:
                exit_code = int(exc.code or 0)
                success = False
            if success or exit_code is not None:
                break
            config = AzurLaneConfig(CONFIG_NAME, task=command)
            application.__dict__["config"] = config
            device.config = config

        after = AzurLaneConfig(CONFIG_NAME, task=command)
        _, next_run_after, _ = _task_due(after, command)
        task_results.append(
            {
                "task": command,
                "status": "completed" if success else "failed",
                "attempts": attempts,
                "next_run_before": next_run_before,
                "next_run_after": next_run_after,
                "exit_code": exit_code,
            }
        )
        if not success:
            break

    verification_config = AzurLaneConfig(CONFIG_NAME, task="DailyQuest")
    application.__dict__["config"] = verification_config
    device.config = verification_config
    verifier = DailyQuestUI(config=verification_config, device=device)
    verifier.claimed_point_reward = False
    all_rewards_claimed = bool(verifier.get_daily_rewards())
    verification_config = AzurLaneConfig(CONFIG_NAME, task="DailyQuest")
    activity = int(verification_config.stored.DailyActivity.value or 0)
    remaining = [
        quest.name for quest in verification_config.stored.DailyQuest.load_quests()
    ]
    failed = [
        item for item in task_results if str(item.get("status", "")).startswith("failed")
    ]
    executed = [item for item in task_results if item.get("status") == "completed"]
    success = not failed and all_rewards_claimed and activity >= 500
    return {
        "success": success,
        "resource_policy": "src-native",
        "tasks": task_results,
        "activity": activity,
        "activity_total": 500,
        "remaining_quests": remaining,
        "all_rewards_claimed": all_rewards_claimed,
        "claimed_during_verification": bool(verifier.claimed_point_reward),
        "work_performed": bool(executed),
        "idempotent": success and not executed and not verifier.claimed_point_reward,
    }


def capture_src_frames(
    root: Path,
    args: argparse.Namespace,
    evidence_root: Path,
) -> dict[str, object]:
    """Capture one real SRC frame without initializing any touch backend."""

    os.chdir(root)
    os.environ["SRC_SCRCPY_MAX_SIZE"] = str(args.scrcpy_max_size)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    _prepend_adb_binary(args.adb)

    from module.base.utils import save_image
    from module.config.config import AzurLaneConfig
    from module.device.device import Device
    from module.device.geometry import Alignment

    config = AzurLaneConfig(CONFIG_NAME, task="Rogue")
    # Capture mode is deliberately read-only.  Avoid MaaTouch/minitouch early
    # initialization even when the persisted Rogue config uses one of them.
    config.Emulator_ControlMethod = "ADB"
    device = Device(config=config)
    try:
        device.screenshot()
        captured_at = datetime.now().astimezone()
        run_dir = (
            evidence_root
            / captured_at.strftime("%Y%m%d-%H%M%S-%f")
        ).resolve()
        run_dir.mkdir(parents=True, exist_ok=False)
        save_image(device.raw_image, run_dir / "raw.png")

        viewports: dict[str, object] = {}
        for alignment in Alignment:
            logical = device.logical_frame(alignment)
            save_image(logical.image, run_dir / f"{alignment.value}.png")
            rect = logical.viewport.capture_rect
            viewports[alignment.value] = {
                "left": rect.left,
                "top": rect.top,
                "right": rect.right,
                "bottom": rect.bottom,
                "width": rect.width,
                "height": rect.height,
            }

        geometry = device.capture_geometry
        metadata = {
            "captured_at": captured_at.isoformat(),
            "frame_id": device.frame_id,
            "screenshot_method": args.screenshot_method,
            "scrcpy_max_size": args.scrcpy_max_size,
            "raw_size": [geometry.width, geometry.height],
            "display_size": [geometry.display.width, geometry.display.height],
            "rotation": geometry.display.rotation,
            "safe_insets": {
                "left": geometry.display.safe_insets.left,
                "top": geometry.display.safe_insets.top,
                "right": geometry.display.safe_insets.right,
                "bottom": geometry.display.safe_insets.bottom,
            },
            "logical_size": list(LOGICAL_RESOLUTION),
            "viewports": viewports,
            "files": {
                "raw": str(run_dir / "raw.png"),
                **{
                    alignment.value: str(run_dir / f"{alignment.value}.png")
                    for alignment in Alignment
                },
            },
        }
        _atomic_json_write(run_dir / "geometry.json", metadata)
        return {"path": str(run_dir), **metadata}
    finally:
        if args.screenshot_method == "scrcpy":
            device._scrcpy_server_stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a bounded StarRailCopilot workflow against an Android device"
    )
    parser.add_argument("--upstream", required=True, type=Path)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--server", choices=tuple(SERVER_PACKAGES), default="CN-Official")
    parser.add_argument(
        "--game-language",
        choices=GAME_LANGUAGES,
        default="cn",
        help="SRC OCR language; use auto only when the current map name is known",
    )
    parser.add_argument(
        "--screenshot-method", choices=SCREENSHOT_METHODS, default="scrcpy"
    )
    parser.add_argument(
        "--scrcpy-max-size",
        choices=SCRCPY_MAX_SIZES,
        default=DEFAULT_SCRCPY_MAX_SIZE,
        type=int,
    )
    parser.add_argument("--control-method", choices=CONTROL_METHODS, default="MaaTouch")
    parser.add_argument("--workflow", choices=WORKFLOWS, default="rogue")
    parser.add_argument("--world", choices=ROGUE_WORLDS, default=ROGUE_WORLDS[-1])
    parser.add_argument("--path", choices=ROGUE_PATHS, default="The_Hunt")
    parser.add_argument(
        "--domain-strategy", choices=DOMAIN_STRATEGIES, default="combat"
    )
    parser.add_argument(
        "--use-immersifier",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--double-event",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--weekly-farming",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument(
        "--use-stamina",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=Path("debug") / "starrailcopilot-device",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--capture", action="store_true")
    mode.add_argument("--run", action="store_true")
    return parser


def _summary(
    *,
    status: str,
    upstream: dict[str, object],
    device: dict[str, object],
    args: argparse.Namespace,
) -> dict[str, object]:
    return {
        "status": status,
        "upstream": upstream,
        "device": {
            key: device.get(key)
            for key in (
                "serial",
                "server",
                "package",
                "device_state",
                "package_installed",
            )
        },
        "screen": {
            key: device.get(key)
            for key in (
                "screen_state",
                "game_ready",
                "foreground_package",
                "foreground_activity",
                "foreground_matches",
                "width",
                "height",
                "orientation",
                "orientation_matches",
                "resolution_supported",
                "resolution_mode",
                "adaptive_geometry_available",
                "projected_capture_width",
                "projected_capture_height",
                "capture_resolution_supported",
                "required_resolution",
                "adb_error",
            )
        },
        "adapter": {
            "native_android_stack": True,
            "capture": args.screenshot_method,
            "control": args.control_method,
            "desktop_api_emulation": False,
            "logical_resolution": list(LOGICAL_RESOLUTION),
            "adaptive_geometry": upstream.get("adaptive_geometry", {}),
            "scrcpy_max_size": args.scrcpy_max_size,
        },
        "game_language": args.game_language,
        "workflow": args.workflow,
        "rogue": {
            "world": args.world,
            "path": args.path,
            "domain_strategy": args.domain_strategy,
            "use_immersifier": args.use_immersifier,
            "double_event": args.double_event,
            "weekly_farming": args.weekly_farming,
            "use_stamina": args.use_stamina,
        },
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    upstream = validate_upstream_path(args.upstream)
    device = preflight_device(
        adb=args.adb,
        serial=args.serial,
        server=args.server,
        adaptive_geometry=bool(
            dict(upstream.get("adaptive_geometry") or {}).get("available")
        ),
        screenshot_method=args.screenshot_method,
        scrcpy_max_size=args.scrcpy_max_size,
    )
    ready = device.get("game_ready") is True
    status = "ready" if args.preflight and ready else "waiting_for_game"
    if args.capture and ready:
        status = "capturing"
    if args.run and ready:
        status = "running"
    summary = _summary(status=status, upstream=upstream, device=device, args=args)

    if args.preflight:
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return 0
    if not ready:
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return 2
    build_src_config(args.upstream.expanduser().resolve(), args)
    if args.capture:
        summary["status"] = "captured"
        summary["capture_evidence"] = capture_src_frames(
            args.upstream.expanduser().resolve(),
            args,
            args.evidence_dir.expanduser().resolve(),
        )
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return 0
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if args.workflow == "daily":
        result = run_src_daily(args.upstream.expanduser().resolve(), args)
        summary["daily_result"] = result
        success = result.get("success") is True
    else:
        success = run_src_rogue(args.upstream.expanduser().resolve(), args)
    summary["status"] = "completed" if success else "skipped"
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if success else 3


if __name__ == "__main__":
    raise SystemExit(main())
