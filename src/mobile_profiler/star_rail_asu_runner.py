"""Run Auto_Simulated_Universe behind an Android compatibility boundary.

This module is launched as a separate process.  The external checkout remains
licensed and distributed by its upstream AGPL-3.0 project; Mobile Profiler does
not bundle or import that project in its core process.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Optional, Sequence

from .star_rail_bridge import (
    STAR_RAIL_CN_PACKAGE,
    StarRailAdbBridge,
)


UPSTREAM_REPOSITORY = "https://github.com/CHNZYX/Auto_Simulated_Universe"
UPSTREAM_LICENSE = "AGPL-3.0"


def validate_upstream_path(path: Path) -> dict[str, object]:
    root = path.expanduser().resolve()
    required = (
        root / "simul.py",
        root / "utils" / "simul" / "utils.py",
        root / "utils" / "models" / "v3_det.onnx",
        root / "utils" / "models" / "v4_rec.onnx",
        root / "imgs" / "money.jpg",
        root / "LICENSE",
    )
    missing = [str(item.relative_to(root)) for item in required if not item.is_file()]
    if missing:
        raise RuntimeError(
            "Auto_Simulated_Universe checkout is incomplete: " + ", ".join(missing)
        )
    license_text = (root / "LICENSE").read_text(encoding="utf-8", errors="replace")
    if "GNU AFFERO GENERAL PUBLIC LICENSE" not in license_text:
        raise RuntimeError("Auto_Simulated_Universe checkout has an unexpected license")
    commit = ""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
        if result.returncode == 0:
            commit = result.stdout.decode("ascii", errors="ignore").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    map_roots = [item for item in (root / "imgs" / "maps").iterdir() if item.is_dir()]
    return {
        "path": str(root),
        "repository": UPSTREAM_REPOSITORY,
        "license": UPSTREAM_LICENSE,
        "commit": commit,
        "map_count": len(map_roots),
    }


def _module(name: str, **attributes: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def install_android_compatibility(bridge: StarRailAdbBridge) -> None:
    """Inject only the desktop API surface used by the external runtime."""

    width, height = bridge.window_size()

    class _Shell:
        def SendKeys(self, _value: str) -> None:  # noqa: N802 - upstream API
            return None

    class _AdbScreen:
        def __init__(self, _width: int = width, _height: int = height) -> None:
            self.width = width
            self.height = height

        def grab(self, _x: int = 0, _y: int = 0):
            return bridge.capture()

    def _desktop_screenshot():
        from PIL import Image

        frame = bridge.capture()
        return Image.fromarray(frame[:, :, ::-1])

    def _mouse_event(_flag: int, dx: int, dy: int, *_args: object) -> None:
        bridge.camera_move(dx, dy)

    def _drag(dx: float, dy: float, duration: float = 0.4) -> None:
        bridge.drag_from_cursor(dx, dy, duration)

    pyautogui = _module(
        "pyautogui",
        FAILSAFE=False,
        click=lambda *_args, **_kwargs: bridge.click_cursor_or_attack(),
        drag=_drag,
        keyDown=lambda key: bridge.key_down(str(key)),
        keyUp=lambda key: bridge.key_up(str(key)),
        mouseDown=lambda *_args, **_kwargs: None,
        screenshot=_desktop_screenshot,
    )
    setattr(pyautogui, "FAILSAFE", False)
    _module("keyboard", on_press=lambda *_args, **_kwargs: None)
    _module(
        "win32api",
        SetCursorPos=lambda position: bridge.set_cursor(
            (int(position[0]), int(position[1]))
        ),
        mouse_event=_mouse_event,
    )
    win32con = _module(
        "win32con",
        MOUSEEVENTF_MOVE=0x0001,
        LOGPIXELSX=88,
        LOGPIXELSY=90,
    )
    _module(
        "win32gui",
        GetForegroundWindow=lambda: 1,
        GetWindowText=lambda _hwnd: "崩坏：星穹铁道",
        GetClientRect=lambda _hwnd: (0, 0, width, height),
        GetWindowRect=lambda _hwnd: (0, 0, width, height),
        GetWindowDC=lambda _hwnd: 1,
        ReleaseDC=lambda *_args: None,
        FindWindow=lambda *_args: 1,
        SetForegroundWindow=lambda *_args: None,
        ShowWindow=lambda *_args: None,
        GetClassName=lambda _hwnd: "UnityWndClass",
        EnumWindows=lambda callback, value: callback(1, value),
    )
    _module("win32print", GetDeviceCaps=lambda _dc, _index: 96)
    _module("pythoncom", CoInitialize=lambda: None)
    _module("pywintypes")
    _module(
        "pyuac",
        isUserAdmin=lambda: True,
        runAsAdmin=lambda *_args, **_kwargs: None,
    )
    client = _module("win32com.client", Dispatch=lambda *_args, **_kwargs: _Shell())
    win32com = _module("win32com", client=client)
    setattr(win32com, "client", client)
    screenshot = _module("utils.screenshot", Screen=_AdbScreen)
    setattr(screenshot, "Screen", _AdbScreen)
    # Keep linters and debuggers aware that the constants are intentionally live.
    setattr(win32con, "MOUSEEVENTF_MOVE", 0x0001)


def load_upstream_runtime(root: Path, bridge: StarRailAdbBridge):
    root = root.expanduser().resolve()
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    install_android_compatibility(bridge)
    simul = importlib.import_module("simul")
    universe_utils = importlib.import_module("utils.simul.utils")
    keyops = importlib.import_module("utils.simul.keyops")
    original_init = universe_utils.UniverseUtils.__init__

    def _android_init(instance) -> None:
        original_init(instance)
        width, height = bridge.window_size()
        instance.x0 = 0
        instance.y0 = 0
        instance.x1 = width
        instance.y1 = height
        instance.xx = width
        instance.yy = height
        instance.full = False
        # Templates are authored for 1080 px height.  Keep their aspect ratio
        # and let each expected ROI absorb the phone's extra horizontal space.
        instance.scx = height / 1080.0
        instance.scy = height / 1080.0
        instance.scale = 1.0
        instance.real_width = width
        instance.sct = sys.modules["utils.screenshot"].Screen(width, height)

    universe_utils.UniverseUtils.__init__ = _android_init
    keyops.keyDown = lambda key: bridge.key_down(str(key))
    keyops.keyUp = lambda key: bridge.key_up(str(key))
    return simul, universe_utils


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run external Auto_Simulated_Universe against an Android phone"
    )
    parser.add_argument("--upstream", required=True, type=Path)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--package", default=STAR_RAIL_CN_PACKAGE)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--speed", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--bonus", action="store_true")
    return parser


def classify_game_screen(runner, bridge: StarRailAdbBridge) -> dict[str, object]:
    """Use the upstream OCR model to gate execution on a ready game screen."""

    frame = bridge.capture()
    try:
        rows = runner.ts.ts.ocr(frame)
    except Exception as exc:
        return {
            "screen_state": "vision_error",
            "game_ready": False,
            "recognized_text": [],
            "vision_error": str(exc),
        }
    texts: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            text = str(row[1][0] or "").strip()
        except (IndexError, KeyError, TypeError):
            continue
        if text:
            texts.append(text)
    joined = " ".join(texts)
    if "资源下载" in joined or "下载中" in joined:
        state = "resource_downloading"
    elif "开始游戏" in joined or "点击进入" in joined:
        state = "start_screen"
    elif "模拟宇宙" in joined or "差分宇宙" in joined:
        state = "universe_ui"
    elif "UID" in joined:
        state = "in_game"
    else:
        state = "unknown"
    safe_texts = [text for text in texts if "UID" not in text.upper()][:16]
    return {
        "screen_state": state,
        "game_ready": state in {"in_game", "universe_ui"},
        "recognized_text": safe_texts,
        "vision_error": "",
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.preflight and not args.run:
        raise ValueError("choose --preflight or --run")
    upstream = validate_upstream_path(args.upstream)
    bridge = StarRailAdbBridge(
        args.serial,
        adb=args.adb,
        package=args.package,
    )
    device = bridge.preflight()
    if (
        device.get("foreground_matches") is not True
        or device.get("orientation_matches") is not True
    ):
        screen_state = (
            "wrong_app"
            if device.get("foreground_matches") is not True
            else "wrong_orientation"
        )
        summary = {
            "status": "waiting_for_game",
            "upstream": upstream,
            "device": device,
            "screen": {
                "screen_state": screen_state,
                "game_ready": False,
                "recognized_text": [],
                "vision_error": "",
            },
            "adapter": {
                "capture": "adb_exec_out",
                "touch": "uiautomator2_persistent_touch",
                "template_scale": None,
                "screen": [device.get("width"), device.get("height")],
                "loaded_map_count": upstream.get("map_count", 0),
                "ocr": "not_loaded",
            },
        }
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        bridge.stop()
        return 0 if args.preflight else 1
    simul, _universe_utils = load_upstream_runtime(args.upstream, bridge)
    runner = simul.SimulatedUniverse(
        find=1,
        debug=int(args.debug),
        show_map=0,
        speed=int(args.speed),
        consumable=0,
        slow=0,
        nums=1,
        unlock=True,
        bonus=bool(args.bonus),
        update=0,
        gui=0,
    )
    screen = classify_game_screen(runner, bridge)
    summary = {
        "status": "ready" if args.preflight and not args.run else "running",
        "upstream": upstream,
        "device": device,
        "screen": screen,
        "adapter": {
            "capture": "adb_exec_out",
            "touch": "uiautomator2_persistent_touch",
            "template_scale": runner.scx,
            "screen": [runner.xx, runner.yy],
            "loaded_map_count": len(runner.img_set),
            "ocr": type(runner.ts).__name__,
        },
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if not args.run:
        bridge.stop()
        return 0
    try:
        runner.start()
    finally:
        bridge.stop()
    return 0 if runner.end else 1


if __name__ == "__main__":
    raise SystemExit(main())
