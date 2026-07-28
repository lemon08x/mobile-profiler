from __future__ import annotations

import unittest
from pathlib import Path

from mobile_profiler.maaend_guard import load_maaend_guard_policy
from mobile_profiler.maaend_task_trial import (
    build_task_configurations,
    build_task_payload,
    evaluate_task_snapshot,
)


class MaaEndTaskTrialTests(unittest.TestCase):
    def test_payload_requires_exact_authorization_and_auditable_terminal(self) -> None:
        policy = load_maaend_guard_policy()
        with self.assertRaisesRegex(ValueError, "--authorize-task DailyRewards"):
            build_task_payload(
                runtime_root=Path("runtime"),
                device="SERIAL-001",
                tasks=["DailyRewards"],
                authorized_tasks=[],
                policy=policy,
            )
        allowed = build_task_payload(
            runtime_root=Path("runtime"),
            device="SERIAL-001",
            tasks=["DailyRewards"],
            authorized_tasks=["DailyRewards"],
            policy=policy,
        )
        self.assertEqual(allowed["authorized_tasks"], ["DailyRewards"])

        with self.assertRaisesRegex(ValueError, "unselected task"):
            build_task_payload(
                runtime_root=Path("runtime"),
                device="SERIAL-001",
                tasks=["PullCountCalculator"],
                authorized_tasks=["DailyRewards"],
                policy=policy,
            )
        with self.assertRaisesRegex(ValueError, "auditable natural terminal"):
            build_task_payload(
                runtime_root=Path("runtime"),
                device="SERIAL-001",
                tasks=["AutoEssence"],
                authorized_tasks=["AutoEssence"],
                policy=policy,
            )

    def test_task_configuration_rejects_options_for_other_tasks(self) -> None:
        rows = build_task_configurations(
            ["PullCountCalculator"],
            {"PullCountCalculator": {}},
        )
        self.assertEqual(rows[0]["name"], "PullCountCalculator")
        with self.assertRaisesRegex(ValueError, "unselected task"):
            build_task_configurations(
                ["PullCountCalculator"],
                {"DailyRewards": {}},
            )

    def test_evaluation_requires_every_independent_gate(self) -> None:
        snapshot = {
            "status": "completed",
            "guarded_flow_verified": True,
            "mxu_api": {
                "tasks": [
                    {"name": "PullCountCalculator", "status": "succeeded"}
                ]
            },
            "terminal_contract": {
                "verified": True,
                "tasks": [
                    {
                        "name": "PullCountCalculator",
                        "contract": "pipeline_terminal",
                        "passed": True,
                    }
                ],
            },
            "runtime_integrity": {"verified": True},
            "guard": {"last_decision": {"allowed": True}},
        }
        result = evaluate_task_snapshot(snapshot, ["PullCountCalculator"])
        self.assertTrue(result["successful"])

        snapshot["terminal_contract"]["tasks"][0]["passed"] = False
        failed = evaluate_task_snapshot(snapshot, ["PullCountCalculator"])
        self.assertFalse(failed["successful"])
        self.assertIn("all_terminal_contracts_passed", failed["reasons"])

    def test_evaluation_rejects_extra_or_reordered_mxu_tasks(self) -> None:
        snapshot = {
            "status": "completed",
            "guarded_flow_verified": True,
            "mxu_api": {
                "tasks": [
                    {"name": "DailyRewards", "status": "succeeded"},
                    {"name": "PullCountCalculator", "status": "succeeded"},
                ]
            },
            "terminal_contract": {
                "verified": True,
                "tasks": [
                    {"name": "PullCountCalculator", "passed": True},
                    {"name": "DailyRewards", "passed": True},
                ],
            },
            "runtime_integrity": {"verified": True},
            "guard": {"last_decision": {"allowed": True}},
        }
        result = evaluate_task_snapshot(
            snapshot,
            ["PullCountCalculator", "DailyRewards"],
        )
        self.assertFalse(result["successful"])
        self.assertFalse(result["checks"]["exact_mxu_task_set"])


if __name__ == "__main__":
    unittest.main()
