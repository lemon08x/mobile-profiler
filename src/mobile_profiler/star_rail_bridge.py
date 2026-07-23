"""ADB touch bridge for the external Auto_Simulated_Universe runtime.

The upstream project remains a separate AGPL process and resource directory.
This module only translates its public keyboard/mouse style operations into
allowlisted Android screenshot and touch operations.  It does not import the
upstream project or ship any of its game templates.
"""

from __future__ import annotations

import math
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


STAR_RAIL_CN_PACKAGE = "com.miHoYo.hkrpg"
STAR_RAIL_OVERSEA_PACKAGE = "com.HoYoverse.hkrpgoversea"
STAR_RAIL_PACKAGES = frozenset(
    {STAR_RAIL_CN_PACKAGE, STAR_RAIL_OVERSEA_PACKAGE}
)


def _ratio(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0.0 or result > 1.0:
        raise ValueError(f"{name} must be within 0..1")
    return result


@dataclass(frozen=True)
class StarRailTouchLayout:
    """Normalized Android control positions, calibrated for landscape play."""

    joystick_x: float = 0.105
    joystick_y: float = 0.790
    joystick_radius: float = 0.065
    camera_x: float = 0.670
    camera_y: float = 0.470
    camera_span: float = 0.150
    camera_gain: float = 1.15
    attack_x: float = 0.910
    attack_y: float = 0.790
    sprint_x: float = 0.905
    sprint_y: float = 0.625
    interact_x: float = 0.825
    interact_y: float = 0.570
    technique_x: float = 0.825
    technique_y: float = 0.790
    auto_battle_x: float = 0.930
    auto_battle_y: float = 0.095
    resonance_x: float = 0.750
    resonance_y: float = 0.785
    map_x: float = 0.055
    map_y: float = 0.080
    inventory_x: float = 0.105
    inventory_y: float = 0.080
    party_x: float = 0.955
    party_y_start: float = 0.310
    party_y_step: float = 0.145

    def __post_init__(self) -> None:
        ratio_fields = (
            "joystick_x",
            "joystick_y",
            "joystick_radius",
            "camera_x",
            "camera_y",
            "camera_span",
            "attack_x",
            "attack_y",
            "sprint_x",
            "sprint_y",
            "interact_x",
            "interact_y",
            "technique_x",
            "technique_y",
            "auto_battle_x",
            "auto_battle_y",
            "resonance_x",
            "resonance_y",
            "map_x",
            "map_y",
            "inventory_x",
            "inventory_y",
            "party_x",
            "party_y_start",
            "party_y_step",
        )
        for field_name in ratio_fields:
            _ratio(getattr(self, field_name), field_name)
        if not math.isfinite(self.camera_gain) or self.camera_gain <= 0:
            raise ValueError("camera_gain must be positive")


class StarRailAdbBridge:
    """Translate desktop-like game input into deterministic Android touches."""

    _MOVEMENT_KEYS = frozenset({"w", "a", "s", "d"})

    def __init__(
        self,
        serial: str,
        *,
        adb: str = "adb",
        package: str = STAR_RAIL_CN_PACKAGE,
        layout: Optional[StarRailTouchLayout] = None,
        connect_factory: Optional[Callable[[str], object]] = None,
        capture_func: Optional[Callable[[str, str], tuple[bytes, int, int]]] = None,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        self.serial = str(serial or "").strip()
        if not self.serial:
            raise ValueError("Star Rail ADB bridge requires a device serial")
        self.adb = str(adb or "adb").strip() or "adb"
        self.package = str(package or "").strip()
        if self.package not in STAR_RAIL_PACKAGES:
            raise ValueError(f"unsupported Star Rail package: {self.package}")
        self.layout = layout or StarRailTouchLayout()
        self._connect_factory = connect_factory
        self._capture_func = capture_func
        self._sleep = sleep_func
        self._device: Optional[object] = None
        self._width = 0
        self._height = 0
        self._movement_keys: set[str] = set()
        self._action_keys: set[str] = set()
        self._movement_active = False
        self._movement_point = (0, 0)
        self._cursor = (0, 0)
        self._cursor_pending = False
        self._lock = threading.RLock()
        self.last_action = ""
        self.last_error = ""

    def _connect(self) -> object:
        if self._connect_factory is not None:
            return self._connect_factory(self.serial)
        try:
            import uiautomator2 as u2  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "uiautomator2 is required for persistent Star Rail touch input"
            ) from exc
        return u2.connect(self.serial)

    @property
    def device(self) -> object:
        with self._lock:
            if self._device is None:
                self._device = self._connect()
            return self._device

    def window_size(
        self,
        *,
        refresh: bool = False,
        require_landscape: bool = True,
    ) -> tuple[int, int]:
        with self._lock:
            if refresh or self._width <= 0 or self._height <= 0:
                width, height = getattr(self.device, "window_size")()
                self._width, self._height = int(width), int(height)
            if require_landscape and self._width <= self._height:
                raise RuntimeError(
                    f"Star Rail must be landscape, got {self._width}x{self._height}"
                )
            return self._width, self._height

    def _pixel(self, x: float, y: float) -> tuple[int, int]:
        width, height = self.window_size()
        return (
            max(0, min(width - 1, int(round(_ratio(x, "x") * (width - 1))))),
            max(0, min(height - 1, int(round(_ratio(y, "y") * (height - 1))))),
        )

    def foreground(self) -> dict[str, str]:
        current = getattr(self.device, "app_current")()
        if not isinstance(current, dict):
            current = {}
        return {
            "package": str(current.get("package") or ""),
            "activity": str(current.get("activity") or ""),
        }

    def preflight(self) -> dict[str, object]:
        width, height = self.window_size(
            refresh=True,
            require_landscape=False,
        )
        foreground = self.foreground()
        package = foreground["package"]
        landscape = width > height
        return {
            "serial": self.serial,
            "package": self.package,
            "foreground_package": package,
            "foreground_activity": foreground["activity"],
            "foreground_matches": package == self.package,
            "width": width,
            "height": height,
            "orientation": "landscape" if landscape else "portrait",
            "orientation_matches": landscape,
            "persistent_touch": all(
                callable(getattr(getattr(self.device, "touch"), method, None))
                for method in ("down", "move", "up")
            ),
        }

    def capture(self):
        """Return one BGR OpenCV frame from ADB screencap."""

        capture_func = self._capture_func
        if capture_func is None:
            from .adb_agent import capture_adb_screenshot

            capture_func = capture_adb_screenshot
        payload, width, height = capture_func(self.adb, self.serial)
        try:
            import cv2  # type: ignore[import-not-found]
            import numpy
        except ImportError as exc:
            raise RuntimeError("OpenCV and NumPy are required for Star Rail capture") from exc
        frame = cv2.imdecode(numpy.frombuffer(payload, dtype=numpy.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise RuntimeError("ADB returned an invalid Star Rail PNG screenshot")
        if int(frame.shape[1]) != int(width) or int(frame.shape[0]) != int(height):
            raise RuntimeError("ADB screenshot dimensions do not match its PNG header")
        with self._lock:
            self._width, self._height = int(width), int(height)
        return frame

    def _movement_target(self) -> tuple[int, int]:
        x = float("d" in self._movement_keys) - float("a" in self._movement_keys)
        y = float("s" in self._movement_keys) - float("w" in self._movement_keys)
        length = math.hypot(x, y)
        if length:
            x /= length
            y /= length
        return self._pixel(
            self.layout.joystick_x + x * self.layout.joystick_radius,
            self.layout.joystick_y + y * self.layout.joystick_radius,
        )

    def _release_movement(self) -> bool:
        was_active = self._movement_active
        if was_active:
            getattr(self.device, "touch").up(*self._movement_point)
            self._movement_active = False
        return was_active

    def _apply_movement(self) -> None:
        touch = getattr(self.device, "touch")
        if not self._movement_keys:
            self._release_movement()
            return
        target = self._movement_target()
        if not self._movement_active:
            center = self._pixel(self.layout.joystick_x, self.layout.joystick_y)
            touch.down(*center)
            touch.move(*target)
            self._movement_active = True
        else:
            touch.move(*target)
        self._movement_point = target

    def _resume_movement(self, was_active: bool) -> None:
        if was_active and self._movement_keys:
            self._apply_movement()

    def _exclusive_touch(self, callback: Callable[[], None]) -> None:
        with self._lock:
            was_active = self._release_movement()
            try:
                callback()
            finally:
                self._resume_movement(was_active)

    def tap_pixel(self, x: int, y: int) -> None:
        width, height = self.window_size()
        px = max(0, min(width - 1, int(x)))
        py = max(0, min(height - 1, int(y)))
        self._exclusive_touch(lambda: getattr(self.device, "click")(px, py))
        self.last_action = f"tap({px},{py})"

    def tap_ratio(self, x: float, y: float) -> None:
        self.tap_pixel(*self._pixel(x, y))

    def swipe_pixel(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        duration_s: float = 0.12,
    ) -> None:
        width, height = self.window_size()
        sx = max(0, min(width - 1, int(start[0])))
        sy = max(0, min(height - 1, int(start[1])))
        ex = max(0, min(width - 1, int(end[0])))
        ey = max(0, min(height - 1, int(end[1])))
        duration = max(0.02, min(2.0, float(duration_s)))
        self._exclusive_touch(
            lambda: getattr(self.device, "swipe")(sx, sy, ex, ey, duration)
        )
        self.last_action = f"swipe({sx},{sy}->{ex},{ey},{duration:.2f}s)"

    def camera_move(self, dx: float, dy: float = 0.0) -> None:
        width, height = self.window_size()
        start = self._pixel(self.layout.camera_x, self.layout.camera_y)
        maximum = self.layout.camera_span * min(width, height)
        move_x = max(-maximum, min(maximum, -float(dx) * self.layout.camera_gain))
        move_y = max(-maximum, min(maximum, -float(dy) * self.layout.camera_gain))
        end = (int(round(start[0] + move_x)), int(round(start[1] + move_y)))
        self.swipe_pixel(start, end, 0.06)

    def set_cursor(self, position: tuple[int, int]) -> None:
        self._cursor = (int(position[0]), int(position[1]))
        self._cursor_pending = True

    def click_cursor_or_attack(self) -> None:
        if self._cursor_pending:
            self._cursor_pending = False
            self.tap_pixel(*self._cursor)
        else:
            self.tap_ratio(self.layout.attack_x, self.layout.attack_y)

    def drag_from_cursor(self, dx: float, dy: float, duration_s: float = 0.4) -> None:
        start = self._cursor
        self._cursor_pending = False
        end = (int(round(start[0] + dx)), int(round(start[1] + dy)))
        self.swipe_pixel(start, end, duration_s)

    def _tap_control(self, key: str) -> None:
        controls = {
            "shift": (self.layout.sprint_x, self.layout.sprint_y),
            "f": (self.layout.interact_x, self.layout.interact_y),
            "e": (self.layout.technique_x, self.layout.technique_y),
            "v": (self.layout.auto_battle_x, self.layout.auto_battle_y),
            "r": (self.layout.resonance_x, self.layout.resonance_y),
            "m": (self.layout.map_x, self.layout.map_y),
            "b": (self.layout.inventory_x, self.layout.inventory_y),
        }
        if key in controls:
            self.tap_ratio(*controls[key])
            return
        if key in {"1", "2", "3", "4"}:
            index = int(key) - 1
            self.tap_ratio(
                self.layout.party_x,
                self.layout.party_y_start + self.layout.party_y_step * index,
            )
            return
        if key == "esc":
            self._exclusive_touch(lambda: getattr(self.device, "press")("back"))
            self.last_action = "press(back)"
            return
        raise ValueError(f"unsupported Star Rail key: {key}")

    def key_down(self, key: str) -> None:
        normalized = str(key or "").strip().lower()
        if not normalized:
            raise ValueError("Star Rail key cannot be empty")
        with self._lock:
            if normalized in self._MOVEMENT_KEYS:
                self._movement_keys.add(normalized)
                self._apply_movement()
                self.last_action = f"movement_down({normalized})"
                return
            if normalized in self._action_keys:
                return
            self._action_keys.add(normalized)
            self._tap_control(normalized)

    def key_up(self, key: str) -> None:
        normalized = str(key or "").strip().lower()
        with self._lock:
            if normalized in self._MOVEMENT_KEYS:
                self._movement_keys.discard(normalized)
                self._apply_movement()
                self.last_action = f"movement_up({normalized})"
            else:
                self._action_keys.discard(normalized)

    def press(self, key: str, duration_s: float = 0.0) -> None:
        self.key_down(key)
        if duration_s > 0:
            self._sleep(max(0.0, float(duration_s)))
        self.key_up(key)

    def stop(self) -> None:
        with self._lock:
            self._movement_keys.clear()
            self._action_keys.clear()
            try:
                self._release_movement()
                self.last_error = ""
            except Exception as exc:
                self.last_error = str(exc)
                self._movement_active = False
