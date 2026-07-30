#!/usr/bin/env python3
"""Run an MAA Core viewport smoke test on Windows, optionally with one click."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import struct
import time
from pathlib import Path


ASST_OPTION_TOUCH_MODE = 2
ASST_OPTION_VIEWPORT = 7
ASST_MSG_CONNECTION_INFO = 2


def _utf8(value: str | Path) -> bytes:
    return os.fspath(value).encode("utf-8")


def _png_size(payload: bytes) -> tuple[int, int]:
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("MaaCore did not return a PNG screenshot")
    return struct.unpack(">II", payload[16:24])


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--address", required=True)
    parser.add_argument("--config", default="General")
    parser.add_argument(
        "--viewport",
        default="auto",
        help=(
            'Patched MAA viewport: "auto", "hybrid", "adaptive", '
            "JSON [x,y,width,height], or a JSON object."
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--user-dir", type=Path)
    parser.add_argument(
        "--click",
        type=int,
        nargs=2,
        metavar=("X", "Y"),
        help="Optionally issue one MAA logical-coordinate click before the screenshot.",
    )
    parser.add_argument("--post-click-delay", type=float, default=1.0)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    core_root = args.core_root.expanduser().resolve()
    runtime_root = args.runtime_root.expanduser().resolve()
    adb_path = args.adb.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    user_dir = (
        args.user_dir.expanduser().resolve()
        if args.user_dir
        else output_path.parent / "maa-viewport-smoke-user"
    )

    core_path = core_root / "MaaCore.dll"
    if not core_path.is_file():
        raise RuntimeError(f"MaaCore.dll not found: {core_path}")
    if not (runtime_root / "resource").is_dir():
        raise RuntimeError(f"MAA resource directory not found: {runtime_root / 'resource'}")
    if not adb_path.is_file():
        raise RuntimeError(f"adb executable not found: {adb_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    user_dir.mkdir(parents=True, exist_ok=True)

    dll_search = os.add_dll_directory(os.fspath(core_root))
    core = ctypes.WinDLL(os.fspath(core_path))

    callback_type = ctypes.WINFUNCTYPE(None, ctypes.c_int32, ctypes.c_char_p, ctypes.c_void_p)
    connection_events: list[dict[str, object]] = []

    @callback_type
    def callback(message: int, details: bytes | None, _custom: int) -> None:
        if message != ASST_MSG_CONNECTION_INFO or not details:
            return
        try:
            parsed = json.loads(details.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if isinstance(parsed, dict):
            connection_events.append(parsed)

    core.AsstSetUserDir.argtypes = [ctypes.c_char_p]
    core.AsstSetUserDir.restype = ctypes.c_uint8
    core.AsstLoadResource.argtypes = [ctypes.c_char_p]
    core.AsstLoadResource.restype = ctypes.c_uint8
    core.AsstCreateEx.argtypes = [callback_type, ctypes.c_void_p]
    core.AsstCreateEx.restype = ctypes.c_void_p
    core.AsstDestroy.argtypes = [ctypes.c_void_p]
    core.AsstSetInstanceOption.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_char_p]
    core.AsstSetInstanceOption.restype = ctypes.c_uint8
    core.AsstConnect.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
    ]
    core.AsstConnect.restype = ctypes.c_uint8
    core.AsstConnected.argtypes = [ctypes.c_void_p]
    core.AsstConnected.restype = ctypes.c_uint8
    core.AsstAsyncScreencap.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
    core.AsstAsyncScreencap.restype = ctypes.c_int32
    core.AsstAsyncClick.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int32,
        ctypes.c_int32,
        ctypes.c_uint8,
    ]
    core.AsstAsyncClick.restype = ctypes.c_int32
    core.AsstGetImage.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64]
    core.AsstGetImage.restype = ctypes.c_uint64

    handle: int | None = None
    try:
        if not core.AsstSetUserDir(_utf8(user_dir)):
            raise RuntimeError("AsstSetUserDir failed")
        if not core.AsstLoadResource(_utf8(runtime_root)):
            raise RuntimeError("AsstLoadResource failed")

        handle = core.AsstCreateEx(callback, None)
        if not handle:
            raise RuntimeError("AsstCreateEx failed")
        if not core.AsstSetInstanceOption(handle, ASST_OPTION_TOUCH_MODE, b"adb"):
            raise RuntimeError("failed to select the ADB controller")
        if not core.AsstSetInstanceOption(handle, ASST_OPTION_VIEWPORT, args.viewport.encode("utf-8")):
            raise RuntimeError("failed to configure viewport; this MaaCore is probably unpatched")

        connected = bool(
            core.AsstConnect(
                handle,
                _utf8(adb_path),
                args.address.encode("utf-8"),
                args.config.encode("utf-8"),
            )
        )
        if not connected or not core.AsstConnected(handle):
            raise RuntimeError("MaaCore ADB connection failed")

        if args.click:
            click_id = core.AsstAsyncClick(handle, args.click[0], args.click[1], 1)
            if click_id <= 0:
                raise RuntimeError("AsstAsyncClick failed")
            time.sleep(max(0.0, args.post_click_delay))

        async_id = core.AsstAsyncScreencap(handle, 1)
        if async_id <= 0:
            raise RuntimeError("AsstAsyncScreencap failed")

        buffer = ctypes.create_string_buffer(16 * 1024 * 1024)
        image_size = core.AsstGetImage(handle, buffer, len(buffer))
        if image_size <= 0 or image_size > len(buffer):
            raise RuntimeError(f"AsstGetImage returned invalid size: {image_size}")
        payload = bytes(buffer.raw[:image_size])
        width, height = _png_size(payload)
        output_path.write_bytes(payload)

        resolution_event = next(
            (event for event in reversed(connection_events) if event.get("what") == "ResolutionInfo"),
            None,
        )
        print(
            json.dumps(
                {
                    "connected": True,
                    "image": str(output_path),
                    "image_size": [width, height],
                    "viewport": args.viewport,
                    "click": args.click,
                    "resolution_event": resolution_event,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        if handle:
            core.AsstDestroy(handle)
        dll_search.close()


if __name__ == "__main__":
    raise SystemExit(main())
