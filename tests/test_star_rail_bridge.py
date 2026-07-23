from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from mobile_profiler import star_rail_asu_runner
from mobile_profiler.star_rail_bridge import (
    STAR_RAIL_CN_PACKAGE,
    StarRailAdbBridge,
    StarRailTouchLayout,
)


class FakeTouch:
    def __init__(self, events: list[tuple[object, ...]]) -> None:
        self.events = events

    def down(self, x: int, y: int) -> "FakeTouch":
        self.events.append(("down", x, y))
        return self

    def move(self, x: int, y: int) -> "FakeTouch":
        self.events.append(("move", x, y))
        return self

    def up(self, x: int, y: int) -> "FakeTouch":
        self.events.append(("up", x, y))
        return self


class FakeDevice:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []
        self.touch = FakeTouch(self.events)

    def window_size(self) -> tuple[int, int]:
        return 2800, 1260

    def app_current(self) -> dict[str, str]:
        return {
            "package": STAR_RAIL_CN_PACKAGE,
            "activity": "com.mihoyo.combosdk.ComboSDKActivity",
        }

    def click(self, x: int, y: int) -> None:
        self.events.append(("click", x, y))

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration: float,
    ) -> None:
        self.events.append(
            ("swipe", start_x, start_y, end_x, end_y, duration)
        )

    def press(self, key: str) -> None:
        self.events.append(("press", key))


class StarRailAdbBridgeTests(unittest.TestCase):
    def _bridge(self) -> tuple[StarRailAdbBridge, FakeDevice]:
        device = FakeDevice()
        bridge = StarRailAdbBridge(
            "phone-1",
            connect_factory=lambda _serial: device,
            sleep_func=lambda _duration: None,
        )
        return bridge, device

    def test_preflight_reports_landscape_foreground_and_persistent_touch(self) -> None:
        bridge, _device = self._bridge()

        result = bridge.preflight()

        self.assertEqual(result["width"], 2800)
        self.assertEqual(result["height"], 1260)
        self.assertTrue(result["foreground_matches"])
        self.assertTrue(result["persistent_touch"])

    def test_preflight_reports_wrong_app_and_portrait_without_touching(self) -> None:
        bridge, device = self._bridge()
        device.window_size = lambda: (1260, 2800)  # type: ignore[method-assign]
        device.app_current = lambda: {  # type: ignore[method-assign]
            "package": "com.example.other",
            "activity": "OtherActivity",
        }

        result = bridge.preflight()

        self.assertEqual(result["orientation"], "portrait")
        self.assertFalse(result["orientation_matches"])
        self.assertFalse(result["foreground_matches"])
        self.assertEqual(device.events, [])
        with self.assertRaisesRegex(RuntimeError, "must be landscape"):
            bridge.window_size()

    def test_movement_uses_persistent_joystick_and_releases_on_key_up(self) -> None:
        bridge, device = self._bridge()

        bridge.key_down("w")
        bridge.key_down("d")
        bridge.key_up("w")
        bridge.key_up("d")

        self.assertEqual(device.events[0][0], "down")
        self.assertEqual(device.events[1][0], "move")
        self.assertEqual(device.events[2][0], "move")
        self.assertEqual(device.events[3][0], "move")
        self.assertEqual(device.events[4][0], "up")
        center_y = round(0.790 * 1259)
        self.assertLess(device.events[1][2], center_y)
        self.assertGreater(device.events[2][1], device.events[1][1])

    def test_camera_move_temporarily_releases_and_resumes_movement(self) -> None:
        bridge, device = self._bridge()
        bridge.key_down("w")
        device.events.clear()

        bridge.camera_move(24)

        self.assertEqual([event[0] for event in device.events], [
            "up",
            "swipe",
            "down",
            "move",
        ])
        swipe = device.events[1]
        self.assertLess(swipe[3], swipe[1])

    def test_cursor_click_and_direct_click_use_different_android_controls(self) -> None:
        bridge, device = self._bridge()

        bridge.set_cursor((1400, 630))
        bridge.click_cursor_or_attack()
        bridge.click_cursor_or_attack()

        self.assertEqual(device.events[0], ("click", 1400, 630))
        self.assertEqual(device.events[1][0], "click")
        self.assertGreater(device.events[1][1], 2400)
        self.assertGreater(device.events[1][2], 900)

    def test_named_keys_map_to_allowlisted_touch_controls(self) -> None:
        bridge, device = self._bridge()

        for key in ("f", "e", "v", "r", "m", "b", "1", "4", "esc"):
            bridge.press(key)

        self.assertEqual(len([event for event in device.events if event[0] == "click"]), 8)
        self.assertEqual(device.events[-1], ("press", "back"))
        with self.assertRaisesRegex(ValueError, "unsupported"):
            bridge.press("space")

    def test_layout_and_package_are_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "within 0..1"):
            StarRailTouchLayout(joystick_x=1.2)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            StarRailAdbBridge("phone-1", package="com.example.game")


class StarRailRunnerPreflightTests(unittest.TestCase):
    def test_wrong_foreground_returns_waiting_summary_without_loading_models(self) -> None:
        class Bridge:
            stopped = False

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def preflight(self) -> dict[str, object]:
                return {
                    "serial": "phone-1",
                    "foreground_matches": False,
                    "foreground_package": "com.example.other",
                    "orientation_matches": False,
                    "orientation": "portrait",
                    "width": 1260,
                    "height": 2800,
                    "persistent_touch": True,
                }

            def stop(self) -> None:
                self.stopped = True

        output = io.StringIO()
        with (
            patch.object(
                star_rail_asu_runner,
                "validate_upstream_path",
                return_value={"map_count": 54},
            ),
            patch.object(star_rail_asu_runner, "StarRailAdbBridge", Bridge),
            patch.object(star_rail_asu_runner, "load_upstream_runtime") as load,
            contextlib.redirect_stdout(output),
        ):
            return_code = star_rail_asu_runner.main(
                [
                    "--upstream",
                    ".",
                    "--serial",
                    "phone-1",
                    "--preflight",
                ]
            )

        summary = json.loads(output.getvalue())
        self.assertEqual(return_code, 0)
        self.assertEqual(summary["status"], "waiting_for_game")
        self.assertEqual(summary["screen"]["screen_state"], "wrong_app")
        self.assertFalse(summary["screen"]["game_ready"])
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
