from __future__ import annotations

import unittest
from pathlib import Path

from mobile_profiler.maaend_probe_trial import (
    build_probe_payload,
    evaluate_probe_snapshot,
)


class MaaEndProbeTrialTests(unittest.TestCase):
    def test_input_probe_requires_explicit_authorization(self) -> None:
        with self.assertRaisesRegex(ValueError, "authorize-input"):
            build_probe_payload(
                runtime_root=Path("runtime"),
                device="SERIAL-001",
                probe="ViewportInputProbe",
                authorize_input=False,
            )
        payload = build_probe_payload(
            runtime_root=Path("runtime"),
            device="SERIAL-001",
            probe="ViewportInputProbe",
            authorize_input=True,
        )
        self.assertEqual(payload["authorized_probes"], ["ViewportInputProbe"])
        self.assertIs(payload["allow_viewport_input_probe"], True)

    def test_evaluation_requires_all_independent_gates(self) -> None:
        snapshot = {
            "status": "completed",
            "mxu_api": {
                "tasks": [{"name": "SceneProbe", "status": "succeeded"}]
            },
            "terminal_contract": {
                "verified": True,
                "tasks": [{"name": "SceneProbe", "passed": True}],
            },
            "runtime_integrity": {"verified": True},
        }
        self.assertTrue(
            evaluate_probe_snapshot(snapshot, "SceneProbe")["successful"]
        )
        snapshot["runtime_integrity"] = {"verified": False}
        result = evaluate_probe_snapshot(snapshot, "SceneProbe")
        self.assertFalse(result["successful"])
        self.assertIn("runtime integrity", " ".join(result["reasons"]))


if __name__ == "__main__":
    unittest.main()
