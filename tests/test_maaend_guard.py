from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mobile_profiler.maaend_guard import (
    MaaEndGuardError,
    build_maaend_probe_request,
    evaluate_maaend_guard,
    evaluate_terminal_contracts,
    fingerprint_maaend_resources,
    load_maaend_guard_policy,
    validate_maaend_guard_policy,
)


class MaaEndGuardTests(unittest.TestCase):
    @staticmethod
    def _resource_root(root: Path) -> Path:
        runtime = root / "MaaEnd"
        (runtime / "tasks").mkdir(parents=True)
        (runtime / "resource").mkdir()
        (runtime / "resource_adb").mkdir()
        (runtime / "interface.json").write_text('{"version":"v2.20.0"}', encoding="utf-8")
        (runtime / "tasks" / "DailyRewards.json").write_text("{}", encoding="utf-8")
        (runtime / "resource" / "pipeline.json").write_text("{}", encoding="utf-8")
        (runtime / "resource_adb" / "overlay.json").write_text("{}", encoding="utf-8")
        return runtime

    @staticmethod
    def _profile(name: str, options: dict[str, object] | None = None) -> dict[str, object]:
        return {
            "task_configurations": [
                {
                    "id": "task-1",
                    "name": name,
                    "enabled": True,
                    "option_values": options or {},
                }
            ]
        }

    @staticmethod
    def _request(entry: str = "DailyRewardStart") -> list[dict[str, object]]:
        return [
            {
                "entry": entry,
                "pipeline_override": "[]",
                "selected_task_id": "task-1",
            }
        ]

    def test_checked_in_policy_covers_41_tasks_and_27_adb_tasks(self) -> None:
        policy = load_maaend_guard_policy()
        integration = (
            Path(__file__).resolve().parents[1]
            / "integrations"
            / "maaend"
            / "guard-policy.json"
        )
        packaged = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "mobile_profiler"
            / "maaend_guard_policy.json"
        )

        self.assertEqual(validate_maaend_guard_policy(policy), [])
        self.assertEqual(integration.read_bytes(), packaged.read_bytes())
        self.assertEqual(len(policy["tasks"]), 41)
        self.assertEqual(
            sum(row["adb_supported"] is True for row in policy["tasks"]),
            27,
        )
        baker = next(row for row in policy["tasks"] if row["name"] == "BakerEntry")
        self.assertEqual(baker["authorization"], "baker_entry")
        self.assertEqual(
            {row["name"] for row in policy["internal_probes"]},
            {"SceneProbe", "CaptureUidProbe", "ViewportInputProbe"},
        )
        pull_count = next(
            row for row in policy["tasks"] if row["name"] == "PullCountCalculator"
        )
        self.assertEqual(pull_count["terminal_contract"], "pipeline_terminal")
        self.assertEqual(
            pull_count["terminal_nodes"], ["PullCountCalculatorFinish"]
        )

    def test_pipeline_terminal_policy_requires_unique_terminal_nodes(self) -> None:
        policy = load_maaend_guard_policy()
        pull_count = next(
            row for row in policy["tasks"] if row["name"] == "PullCountCalculator"
        )
        pull_count["terminal_nodes"] = []
        self.assertTrue(
            any(
                "terminal_nodes must be a non-empty string list" in error
                for error in validate_maaend_guard_policy(policy)
            )
        )

        pull_count["terminal_nodes"] = ["Done", "Done"]
        self.assertTrue(
            any(
                "terminal_nodes contains duplicates" in error
                for error in validate_maaend_guard_policy(policy)
            )
        )

    def test_resource_fingerprint_is_deterministic_and_detects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self._resource_root(Path(directory))
            policy = load_maaend_guard_policy()
            first = fingerprint_maaend_resources(runtime, policy)
            cache: dict[str, tuple[int, int, str]] = {}
            second = fingerprint_maaend_resources(
                runtime,
                policy,
                file_hash_cache=cache,
            )
            (runtime / "resource_adb" / "overlay.json").write_text(
                '{"changed":true}',
                encoding="utf-8",
            )
            third = fingerprint_maaend_resources(
                runtime,
                policy,
                file_hash_cache=cache,
            )

        self.assertEqual(first["sha256"], second["sha256"])
        self.assertNotEqual(second["sha256"], third["sha256"])
        self.assertEqual(first["file_count"], 4)

    def test_guard_denies_missing_authorization_unknown_task_and_resource_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self._resource_root(Path(directory))
            policy = load_maaend_guard_policy()
            fingerprint = fingerprint_maaend_resources(runtime, policy)
            policy["resource_fingerprint"]["expected_sha256"] = fingerprint["sha256"]

            with self.assertRaises(MaaEndGuardError) as denied:
                evaluate_maaend_guard(
                    policy,
                    self._profile("DailyRewards"),
                    {},
                    resource_fingerprint=fingerprint,
                    task_requests=self._request(),
                )
            self.assertEqual(denied.exception.code, "task_denied")

            with self.assertRaises(MaaEndGuardError) as unknown:
                evaluate_maaend_guard(
                    policy,
                    self._profile("UnknownTask"),
                    {"authorized_tasks": ["UnknownTask"]},
                    resource_fingerprint=fingerprint,
                    task_requests=self._request("UnknownStart"),
                )
            self.assertEqual(unknown.exception.code, "task_denied")

            drifted = dict(fingerprint)
            drifted["sha256"] = "f" * 64
            with self.assertRaises(MaaEndGuardError) as drift:
                evaluate_maaend_guard(
                    policy,
                    self._profile("DailyRewards"),
                    {"authorized_tasks": ["DailyRewards"]},
                    resource_fingerprint=drifted,
                    task_requests=self._request(),
                )
            self.assertEqual(drift.exception.code, "resource_drift")

    def test_guard_pins_options_request_and_baker_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self._resource_root(Path(directory))
            policy = load_maaend_guard_policy()
            fingerprint = fingerprint_maaend_resources(runtime, policy)
            policy["resource_fingerprint"]["expected_sha256"] = fingerprint["sha256"]
            first = evaluate_maaend_guard(
                policy,
                self._profile("DailyRewards", {"Mode": "Safe"}),
                {"authorized_tasks": ["DailyRewards"]},
                resource_fingerprint=fingerprint,
                task_requests=self._request(),
            )
            second = evaluate_maaend_guard(
                policy,
                self._profile("DailyRewards", {"Mode": "Fast"}),
                {"authorized_tasks": ["DailyRewards"]},
                resource_fingerprint=fingerprint,
                task_requests=self._request(),
            )
            with self.assertRaises(MaaEndGuardError):
                evaluate_maaend_guard(
                    policy,
                    self._profile("BakerEntry"),
                    {"authorized_tasks": ["BakerEntry"]},
                    resource_fingerprint=fingerprint,
                    task_requests=self._request("BakerEntry"),
                )
            baker = evaluate_maaend_guard(
                policy,
                self._profile("BakerEntry"),
                {
                    "authorized_tasks": ["BakerEntry"],
                    "allow_baker_entry": True,
                },
                resource_fingerprint=fingerprint,
                task_requests=self._request("BakerEntry"),
            )

        self.assertNotEqual(first["decision_sha256"], second["decision_sha256"])
        self.assertTrue(baker["allowed"])

    def test_guard_rejects_task_entry_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self._resource_root(Path(directory))
            policy = load_maaend_guard_policy()
            fingerprint = fingerprint_maaend_resources(runtime, policy)
            policy["resource_fingerprint"]["expected_sha256"] = fingerprint["sha256"]
            with self.assertRaises(MaaEndGuardError) as drift:
                evaluate_maaend_guard(
                    policy,
                    self._profile("DailyRewards"),
                    {"authorized_tasks": ["DailyRewards"]},
                    resource_fingerprint=fingerprint,
                    task_requests=self._request("UnexpectedEntry"),
                )

        self.assertEqual(drift.exception.code, "task_denied")
        self.assertEqual(drift.exception.evidence["tasks"][0]["reason"], "task_entry_drift")

    def test_internal_probe_builder_and_guard_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self._resource_root(Path(directory))
            policy = load_maaend_guard_policy()
            fingerprint = fingerprint_maaend_resources(runtime, policy)
            policy["resource_fingerprint"]["expected_sha256"] = fingerprint["sha256"]
            profile = {"task_configurations": []}
            scene_request = build_maaend_probe_request(policy, "SceneProbe")
            scene = evaluate_maaend_guard(
                policy,
                profile,
                {"internal_probe": "SceneProbe"},
                resource_fingerprint=fingerprint,
                task_requests=[scene_request],
            )
            input_request = build_maaend_probe_request(
                policy,
                "ViewportInputProbe",
            )
            with self.assertRaises(MaaEndGuardError) as missing_authorization:
                evaluate_maaend_guard(
                    policy,
                    profile,
                    {"internal_probe": "ViewportInputProbe"},
                    resource_fingerprint=fingerprint,
                    task_requests=[input_request],
                )
            allowed = evaluate_maaend_guard(
                policy,
                profile,
                {
                    "internal_probe": "ViewportInputProbe",
                    "authorized_probes": ["ViewportInputProbe"],
                    "allow_viewport_input_probe": True,
                },
                resource_fingerprint=fingerprint,
                task_requests=[input_request],
            )
            with self.assertRaises(MaaEndGuardError) as drift:
                evaluate_maaend_guard(
                    policy,
                    profile,
                    {"internal_probe": "SceneProbe"},
                    resource_fingerprint=fingerprint,
                    task_requests=[{**scene_request, "entry": "UnexpectedEntry"}],
                )
            with self.assertRaises(MaaEndGuardError) as nonempty:
                evaluate_maaend_guard(
                    policy,
                    self._profile("DailyRewards"),
                    {"internal_probe": "SceneProbe"},
                    resource_fingerprint=fingerprint,
                    task_requests=[scene_request],
                )

        self.assertTrue(scene["allowed"])
        self.assertEqual(scene["mode"], "internal_probe")
        self.assertEqual(
            json.loads(scene_request["pipeline_override"]),
            [{"InWorld": {"next": []}}],
        )
        self.assertEqual(missing_authorization.exception.code, "probe_denied")
        self.assertTrue(allowed["allowed"])
        self.assertEqual(drift.exception.code, "request_mismatch")
        self.assertEqual(nonempty.exception.code, "request_mismatch")

    def test_android_open_game_contract_requires_viewport_gate(self) -> None:
        policy = load_maaend_guard_policy()
        tasks = [
            {
                "name": "AndroidOpenGame",
                "status": "succeeded",
                "maa_task_id": 7,
            }
        ]
        screenshot = {"captured": True, "width": 1280, "height": 720}
        failed = evaluate_terminal_contracts(
            policy,
            tasks,
            terminal_screenshot=screenshot,
            viewport_evidence={"active": True},
            structured_events=[],
            active_contacts=[],
            blocking_incidents=[],
        )
        passed = evaluate_terminal_contracts(
            policy,
            tasks,
            terminal_screenshot=screenshot,
            viewport_evidence={"active": True, "gate_observed": True},
            structured_events=[
                {"kind": "viewport_activated"},
                {
                    "kind": "task_succeeded",
                    "entry": "AndroidOpenGame",
                    "task_id": 7,
                },
            ],
            active_contacts=[],
            blocking_incidents=[],
        )

        self.assertFalse(failed["verified"])
        self.assertTrue(passed["verified"])

    def test_pipeline_entry_contract_requires_exact_framework_completion(self) -> None:
        policy = load_maaend_guard_policy()
        definition = next(
            row for row in policy["tasks"] if row["name"] == "PullCountCalculator"
        )
        definition["terminal_contract"] = "pipeline_entry"
        definition.pop("terminal_nodes")
        tasks = [
            {
                "name": "PullCountCalculator",
                "status": "succeeded",
                "maa_task_id": 21,
            }
        ]
        common = {
            "terminal_screenshot": {"width": 1280, "height": 720},
            "viewport_evidence": {},
            "active_contacts": [],
            "blocking_incidents": [],
        }
        wrong = evaluate_terminal_contracts(
            policy,
            tasks,
            structured_events=[
                {
                    "kind": "task_succeeded",
                    "entry": "DailyRewardStart",
                    "task_id": 21,
                }
            ],
            **common,
        )
        passed = evaluate_terminal_contracts(
            policy,
            tasks,
            structured_events=[
                {
                    "kind": "task_succeeded",
                    "entry": "PullCountCalculatorMain",
                    "task_id": 21,
                }
            ],
            **common,
        )

        self.assertFalse(wrong["verified"])
        self.assertTrue(passed["verified"])

    def test_pipeline_terminal_requires_correlated_entry_and_terminal_node(self) -> None:
        policy = load_maaend_guard_policy()
        task = {
            "name": "PullCountCalculator",
            "status": "succeeded",
            "maa_task_id": 21,
        }
        common = {
            "terminal_screenshot": {"width": 1280, "height": 720},
            "viewport_evidence": {},
            "active_contacts": [],
            "blocking_incidents": [],
        }
        entry = {
            "kind": "task_succeeded",
            "entry": "PullCountCalculatorMain",
            "task_id": 21,
        }
        terminal = {
            "kind": "node_succeeded",
            "name": "PullCountCalculatorFinish",
            "task_id": 21,
        }

        missing_terminal = evaluate_terminal_contracts(
            policy,
            [task],
            structured_events=[entry],
            **common,
        )
        wrong_task = evaluate_terminal_contracts(
            policy,
            [task],
            structured_events=[entry, {**terminal, "task_id": 22}],
            **common,
        )
        passed = evaluate_terminal_contracts(
            policy,
            [task],
            structured_events=[entry, terminal],
            **common,
        )

        self.assertFalse(missing_terminal["verified"])
        self.assertFalse(
            missing_terminal["tasks"][0]["checks"][
                "terminal_node_succeeded_observed"
            ]
        )
        self.assertFalse(wrong_task["verified"])
        self.assertTrue(
            wrong_task["tasks"][0]["checks"]["terminal_node_succeeded_observed"]
        )
        self.assertFalse(
            wrong_task["tasks"][0]["checks"][
                "terminal_node_task_id_correlated"
            ]
        )
        self.assertTrue(passed["verified"])

    def test_pipeline_terminal_accepts_one_of_multiple_declared_nodes(self) -> None:
        policy = load_maaend_guard_policy()
        result = evaluate_terminal_contracts(
            policy,
            [
                {
                    "name": "SellProduct",
                    "status": "succeeded",
                    "maa_task_id": 33,
                }
            ],
            terminal_screenshot={"width": 1280, "height": 720},
            viewport_evidence={},
            structured_events=[
                {
                    "kind": "task_succeeded",
                    "entry": "SellProductSchedule",
                    "task_id": 33,
                },
                {
                    "kind": "node_succeeded",
                    "name": "SellProductTaskEnd",
                    "task_id": 33,
                },
            ],
            active_contacts=[],
            blocking_incidents=[],
        )

        self.assertTrue(result["verified"])

    def test_internal_probe_terminal_contracts_require_labeled_evidence(self) -> None:
        policy = load_maaend_guard_policy()
        screenshot = {"width": 1280, "height": 720}
        common = {
            "terminal_screenshot": screenshot,
            "viewport_evidence": {},
            "active_contacts": [],
            "blocking_incidents": [],
        }
        task_success = {
            "kind": "task_succeeded",
            "entry": "InWorld",
            "task_id": 31,
        }
        scene_recognition = {
            "kind": "node_succeeded",
            "name": "InWorld",
            "recognition": {"alignment": "center", "frame_id": 8},
        }
        scene = evaluate_terminal_contracts(
            policy,
            [{"name": "SceneProbe", "status": "succeeded", "maa_task_id": 31}],
            structured_events=[task_success, scene_recognition],
            **common,
        )
        missing_label = evaluate_terminal_contracts(
            policy,
            [{"name": "SceneProbe", "status": "succeeded", "maa_task_id": 31}],
            structured_events=[
                task_success,
                {
                    **scene_recognition,
                    "recognition": {"alignment": "center", "frame_id": 0},
                },
            ],
            **common,
        )
        capture = evaluate_terminal_contracts(
            policy,
            [
                {
                    "name": "CaptureUidProbe",
                    "status": "succeeded",
                    "maa_task_id": 32,
                }
            ],
            structured_events=[
                {
                    "kind": "task_succeeded",
                    "entry": "AutoStockpileGetUid",
                    "task_id": 32,
                },
                {
                    "kind": "agent_log",
                    "component": "captureuid",
                    "uid_hash_present": True,
                    "uid_hash_sha256": "a" * 64,
                    "alignment": "left",
                    "frame_id": 9,
                },
            ],
            **common,
        )

        self.assertTrue(scene["verified"])
        self.assertFalse(missing_label["verified"])
        self.assertTrue(capture["verified"])

    def test_viewport_input_probe_requires_exact_reversible_touch_trace(self) -> None:
        policy = load_maaend_guard_policy()
        events: list[dict[str, object]] = [
            {
                "kind": "task_succeeded",
                "entry": "InWorld",
                "task_id": 41,
            },
            {
                "kind": "node_succeeded",
                "name": "InWorld",
                "recognition": {"alignment": "center", "frame_id": 10},
            },
            {
                "kind": "agent_log",
                "component": "viewport_input_probe",
                "phase": "joystick_center_released",
                "alignment": "left",
                "frame_id": 10,
                "contact": 0,
            },
            {
                "kind": "agent_log",
                "component": "viewport_input_probe",
                "phase": "camera_reversed",
                "alignment": "right",
                "frame_id": 10,
                "contact": 1,
                "reversed": True,
            },
        ]
        actions = [
            ("touch_down", 0, "left", [195, 551]),
            ("touch_up", 0, "left", [0, 0]),
            ("touch_down", 1, "right", [640, 264]),
            ("touch_move", 1, "right", [664, 264]),
            ("touch_move", 1, "right", [640, 264]),
            ("touch_up", 1, "right", [0, 0]),
        ]
        for action, contact, alignment, point in actions:
            logical: dict[str, object] = {"contact": contact}
            if point is not None:
                logical["point"] = point
            events.append(
                {
                    "kind": "controller_action",
                    "status": "succeeded",
                    "action": action,
                    "param": {"contact": contact},
                    "viewport": {
                        "alignment": alignment,
                        "frame_id": 10,
                        "logical_param": logical,
                    },
                }
            )
        task = {
            "name": "ViewportInputProbe",
            "status": "succeeded",
            "maa_task_id": 41,
        }
        passed = evaluate_terminal_contracts(
            policy,
            [task],
            terminal_screenshot={"width": 1280, "height": 720},
            viewport_evidence={},
            structured_events=events,
            active_contacts=[],
            blocking_incidents=[],
        )
        failed = evaluate_terminal_contracts(
            policy,
            [task],
            terminal_screenshot={"width": 1280, "height": 720},
            viewport_evidence={},
            structured_events=events[:-1],
            active_contacts=[1],
            blocking_incidents=[],
        )

        self.assertTrue(passed["verified"])
        self.assertFalse(failed["verified"])


if __name__ == "__main__":
    unittest.main()
