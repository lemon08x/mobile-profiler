from __future__ import annotations

import unittest

from mobile_profiler.maaend_viewport_trial import evaluate_trial_snapshot


class MaaEndViewportTrialTests(unittest.TestCase):
    def test_requires_task_truth_and_1280x720_terminal_evidence(self) -> None:
        evaluation = evaluate_trial_snapshot(
            {
                "status": "completed",
                "mxu_api": {
                    "tasks": [
                        {"name": "AndroidOpenGame", "status": "succeeded"}
                    ],
                    "screenshots": [
                        {"label": "connection", "width": 720, "height": 1600},
                        {"label": "terminal", "width": 1280, "height": 720},
                    ],
                },
            }
        )
        self.assertTrue(evaluation["successful"])
        self.assertTrue(evaluation["viewport_verified"])

    def test_completed_task_without_terminal_viewport_is_unverified(self) -> None:
        evaluation = evaluate_trial_snapshot(
            {
                "status": "completed",
                "mxu_api": {
                    "tasks": [
                        {"name": "AndroidOpenGame", "status": "succeeded"}
                    ],
                    "screenshots": [
                        {"label": "terminal", "width": 720, "height": 1600}
                    ],
                },
            }
        )
        self.assertFalse(evaluation["successful"])
        self.assertFalse(evaluation["viewport_verified"])
        self.assertIn("not 1280x720", evaluation["reasons"][-1])

    def test_rejects_extra_or_failed_tasks(self) -> None:
        evaluation = evaluate_trial_snapshot(
            {
                "status": "completed",
                "mxu_api": {
                    "tasks": [
                        {"name": "AndroidOpenGame", "status": "succeeded"},
                        {"name": "VisitFriends", "status": "succeeded"},
                    ],
                    "screenshots": [
                        {"label": "terminal", "width": 1280, "height": 720}
                    ],
                },
            }
        )
        self.assertFalse(evaluation["successful"])
        self.assertFalse(evaluation["android_open_game_succeeded"])


if __name__ == "__main__":
    unittest.main()
