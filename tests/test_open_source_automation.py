from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_profiler.open_source_automation import OpenSourceAutomationController


AVAILABLE_RUNTIME = {
    "available": True,
    "detail": "OpenCV test / NumPy test",
    "opencv_version": "test",
    "numpy_version": "test",
}


class FakeFeatureAdapter:
    def __init__(self) -> None:
        self.state = {
            "adapter_id": "fake-runtime",
            "status": "installed",
            "running": False,
            "available": True,
            "device": "",
            "upstream": {
                "repository": "https://example.test/runtime",
                "license": "AGPL-3.0",
                "map_count": 54,
                "disk_bytes": 18_874_368,
                "disk_mib": 18.0,
            },
            "preflight": None,
            "last_error": "",
            "logs": [],
        }
        self.calls: list[tuple[str, object]] = []

    def snapshot(self) -> dict[str, object]:
        return json.loads(json.dumps(self.state))

    def preflight(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("preflight", dict(payload)))
        device = str(payload.get("device") or "")
        self.state.update(
            {
                "status": "ready",
                "device": device,
                "preflight": {
                    "device": {"serial": device},
                    "screen": {"screen_state": "in_game", "game_ready": True},
                },
            }
        )
        return self.snapshot()

    def configure(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("configure", dict(payload)))
        self.state.update({"status": "installed", "device": str(payload.get("device") or "")})
        return self.snapshot()

    def start(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("start", dict(payload)))
        self.state.update({"status": "running", "running": True})
        return self.snapshot()

    def stop(self) -> dict[str, object]:
        self.calls.append(("stop", None))
        self.state.update({"status": "stopped", "running": False})
        return self.snapshot()

    def close(self) -> None:
        self.calls.append(("close", None))


class OpenSourceAutomationControllerTests(unittest.TestCase):
    def _bundle(self, root: Path) -> Path:
        path = root / "bundle.json"
        path.write_text(
            json.dumps(
                {
                    "graph_id": "test-graph",
                    "max_transitions": 4,
                    "templates": [{"id": "home-template", "path": "home.png"}],
                    "states": [{"id": "home", "template": "home-template"}],
                    "transitions": [],
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_snapshot_exposes_bundle_disk_estimate_and_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = OpenSourceAutomationController(root, self._bundle(root))
            snapshot = controller.snapshot()

        projects = {project["id"]: project for project in snapshot["projects"]}
        self.assertEqual(snapshot["bundle"]["graph_id"], "test-graph")
        self.assertEqual(snapshot["bundle"]["max_transitions"], 4)
        self.assertEqual(snapshot["dependency"]["estimated_additional_mib"], 159.6)
        self.assertEqual(snapshot["selection"]["project_ids"], ["maaend"])
        self.assertEqual(snapshot["selection"]["feature_ids"], ["maaend-profile"])
        self.assertTrue(projects["march7th-assistant"]["selectable"])
        self.assertTrue(projects["maaend"]["selectable"])
        self.assertEqual(
            projects["maaend"]["features"][0]["id"],
            "maaend-profile",
        )
        self.assertEqual(set(projects), {"march7th-assistant", "maaend"})
        self.assertEqual(
            [feature["id"] for feature in projects["march7th-assistant"]["features"]],
            ["m7a-universe"],
        )
        self.assertEqual(snapshot["execution"]["status"], "preflight_required")
        self.assertFalse(snapshot["execution"]["can_execute"])
        self.assertEqual(len(snapshot["alignment"]), 6)
        self.assertFalse(snapshot["boundary"]["eval"])
        self.assertFalse(snapshot["boundary"]["arbitrary_shell"])

    def test_update_selection_normalizes_one_project_and_implemented_feature(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeFeatureAdapter()
            controller = OpenSourceAutomationController(
                Path(directory),
                feature_adapters={"m7a-universe": adapter},
            )
            snapshot = controller.update_selection(
                {
                    "project_ids": ["march7th-assistant", "march7th-assistant"],
                    "feature_ids": ["m7a-universe", "m7a-universe"],
                }
            )

        self.assertEqual(snapshot["selection"]["project_ids"], ["march7th-assistant"])
        self.assertEqual(
            snapshot["selection"]["feature_ids"],
            ["m7a-universe"],
        )
        self.assertIsNotNone(snapshot["selection"]["saved_at"])
        self.assertEqual(snapshot["execution"]["status"], "preflight_required")
        self.assertEqual(snapshot["execution"]["selected_feature_count"], 1)
        self.assertEqual(snapshot["logs"][-1]["status"], "configured")

    def test_nested_project_selection_persists_and_dispatches_ready_feature(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = FakeFeatureAdapter()
            controller = OpenSourceAutomationController(
                root,
                feature_adapters={"m7a-universe": adapter},
            )
            snapshot = controller.update_selection(
                {
                    "projects": [
                        {
                            "project_id": "march7th-assistant",
                            "feature_ids": ["m7a-universe"],
                        }
                    ]
                }
            )

            self.assertEqual(
                snapshot["selection"]["projects"],
                [
                    {
                        "project_id": "march7th-assistant",
                        "feature_ids": ["m7a-universe"],
                    }
                ],
            )
            self.assertEqual(snapshot["execution"]["status"], "preflight_required")
            self.assertEqual(
                snapshot["execution"]["runnable_feature_ids"],
                ["m7a-universe"],
            )
            self.assertEqual(
                snapshot["execution"]["pending_feature_ids"],
                [],
            )
            universe = next(
                feature
                for project in snapshot["projects"]
                for feature in project["features"]
                if feature["id"] == "m7a-universe"
            )
            self.assertEqual(universe["adapter_id"], "fake-runtime")
            snapshot = controller.configure(
                {
                    "feature_id": "m7a-universe",
                    "device": "USB-DEVICE",
                    "tasks": [{"name": "Daily", "enabled": True}],
                }
            )
            self.assertEqual(snapshot["adapters"]["m7a-universe"]["device"], "USB-DEVICE")
            snapshot = controller.preflight(
                {
                    "feature_id": "m7a-universe",
                    "device": "USB-DEVICE",
                }
            )
            self.assertTrue(snapshot["execution"]["can_execute"])
            snapshot = controller.start(
                {
                    "feature_id": "m7a-universe",
                    "device": "USB-DEVICE",
                    "speed": True,
                }
            )
            self.assertEqual(snapshot["execution"]["status"], "running")
            snapshot = controller.stop({"feature_id": "m7a-universe"})
            self.assertFalse(snapshot["execution"]["can_stop"])

            persisted = json.loads(
                (root / "open-source-automation" / "selection.json").read_text(
                    encoding="utf-8"
                )
            )
            reloaded = OpenSourceAutomationController(
                root,
                feature_adapters={},
            ).snapshot()

        self.assertEqual(persisted["schema_version"], 2)
        self.assertEqual(
            reloaded["selection"]["feature_ids"],
            ["m7a-universe"],
        )
        self.assertEqual(
            [call[0] for call in adapter.calls],
            ["configure", "preflight", "start", "stop"],
        )

    def test_configurable_missing_runtime_remains_preflightable(self) -> None:
        adapter = FakeFeatureAdapter()
        adapter.state.update(
            {
                "adapter_id": "configurable-runtime",
                "status": "not_installed",
                "available": False,
                "capabilities": {"configure_when_unavailable": True},
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            controller = OpenSourceAutomationController(
                Path(directory),
                feature_adapters={"maaend-profile": adapter},
            )
            snapshot = controller.update_selection(
                {
                    "project_ids": ["maaend"],
                    "feature_ids": ["maaend-profile"],
                }
            )

        self.assertEqual(snapshot["execution"]["runnable_feature_ids"], [])
        self.assertEqual(
            snapshot["execution"]["preflight_feature_ids"],
            ["maaend-profile"],
        )
        self.assertTrue(snapshot["execution"]["can_preflight"])
        self.assertEqual(snapshot["execution"]["status"], "preflight_required")

    def test_update_selection_rejects_multiple_unknown_and_orphan_projects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = OpenSourceAutomationController(Path(directory))
            with self.assertRaisesRegex(ValueError, "only one"):
                controller.update_selection(
                    {
                        "project_ids": ["maaend", "march7th-assistant"],
                        "feature_ids": [],
                    }
                )
            with self.assertRaisesRegex(ValueError, "unknown"):
                controller.update_selection(
                    {"project_ids": ["maa"], "feature_ids": []}
                )
            with self.assertRaisesRegex(ValueError, "selected project"):
                controller.update_selection(
                    {"project_ids": [], "feature_ids": ["m7a-universe"]}
                )

    def test_run_demo_updates_result_evidence_and_log(self) -> None:
        fake_result = {
            "iterations": 7,
            "frame": {"width": 720, "height": 1280},
            "matched": True,
            "score": 1.0,
            "threshold": 0.98,
            "scale": 1.0,
            "bounds": [438, 846, 582, 942],
            "expected_bounds": [438, 846, 582, 942],
            "coordinate_exact": True,
            "mean_ms": 12.5,
            "p95_ms": 16.7,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = OpenSourceAutomationController(root, self._bundle(root))
            with (
                patch(
                    "mobile_profiler.open_source_automation.probe_image_runtime",
                    return_value=AVAILABLE_RUNTIME,
                ),
                patch(
                    "mobile_profiler.open_source_automation.run_synthetic_visual_demo",
                    return_value=fake_result,
                ) as run_demo,
            ):
                snapshot = controller.run_demo({"iterations": 7})

            result = snapshot["demo"]["result"]
            result_file = root / "open-source-automation" / "result.json"
            persisted = json.loads(result_file.read_text(encoding="utf-8"))

        run_demo.assert_called_once()
        self.assertEqual(snapshot["status"], "completed")
        self.assertTrue(result["coordinate_exact"])
        self.assertIn("overlay", result["evidence"])
        self.assertEqual(persisted["iterations"], 7)
        self.assertEqual(snapshot["logs"][-1]["status"], "completed")

    def test_run_demo_validates_iteration_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = OpenSourceAutomationController(Path(directory))
            with self.assertRaisesRegex(ValueError, "1..100"):
                controller.run_demo({"iterations": 101})

    def test_run_demo_reports_missing_optional_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = OpenSourceAutomationController(Path(directory))
            with patch(
                "mobile_profiler.open_source_automation.probe_image_runtime",
                return_value={
                    "available": False,
                    "detail": "缺少可选图像运行时：OpenCV",
                    "opencv_version": "",
                    "numpy_version": "",
                },
            ):
                with self.assertRaisesRegex(RuntimeError, "image"):
                    controller.run_demo({"iterations": 1})


if __name__ == "__main__":
    unittest.main()
