from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mobile_profiler.maa_iteration import (
    IncidentSignal,
    read_json_object,
    record_incident_bundle,
    write_json_atomic,
)
from mobile_profiler.maaend_iteration import (
    audit_maaend_agent_source,
    audit_maa_framework_go_source,
    audit_maaframework_source,
    promote_maaend_incident_fixture,
    validate_maaend_policy_copies,
)


class MaaEndIterationTests(unittest.TestCase):
    def test_framework_audit_fails_closed_on_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = audit_maaframework_source(Path(directory))
        self.assertFalse(result["valid"])
        self.assertGreaterEqual(len(result["failures"]), 5)

    def test_agent_audit_fails_closed_on_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = audit_maaend_agent_source(Path(directory))
        self.assertFalse(result["valid"])
        self.assertGreaterEqual(len(result["failures"]), 2)

    def test_go_binding_audit_fails_closed_on_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = audit_maa_framework_go_source(Path(directory))
        self.assertFalse(result["valid"])
        self.assertGreaterEqual(len(result["failures"]), 3)

    def test_guard_policy_copies_must_be_valid_and_byte_identical(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "integrations"
            / "maaend"
            / "guard-policy.json"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            integration = root / "integration.json"
            packaged = root / "packaged.json"
            integration.write_bytes(source.read_bytes())
            packaged.write_bytes(source.read_bytes())
            valid = validate_maaend_policy_copies(integration, packaged)
            packaged.write_bytes(source.read_bytes() + b"\n")
            drifted = validate_maaend_policy_copies(integration, packaged)

        self.assertTrue(valid["valid"])
        self.assertTrue(valid["byte_identical"])
        self.assertFalse(drifted["valid"])
        self.assertFalse(drifted["byte_identical"])

    def test_fixture_promotion_requires_explicit_redaction_confirmation(self) -> None:
        signal = IncidentSignal.create(
            "maaend_static_screen",
            "static",
            {"task": "SceneProbe"},
            stop=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            record_incident_bundle(run_dir, signal, screenshot=b"png")
            incident = next((run_dir / "incidents").glob("*/incident.json"))
            manifest = root / "fixtures" / "manifest.json"
            write_json_atomic(
                manifest,
                {
                    "schema_version": 1,
                    "logical_image_size": [1280, 720],
                    "fixtures": [],
                },
            )
            with self.assertRaisesRegex(ValueError, "explicit confirmation"):
                promote_maaend_incident_fixture(
                    incident,
                    manifest,
                    fixture_id="scene-001",
                    page="world",
                    expected_task="SceneProbe",
                    expected_alignment="center",
                    confirmed_redacted=False,
                )
            entry = promote_maaend_incident_fixture(
                incident,
                manifest,
                fixture_id="scene-001",
                page="world",
                expected_task="SceneProbe",
                expected_alignment="center",
                confirmed_redacted=True,
                redaction_note="UID masked",
            )
            persisted = read_json_object(manifest)["fixtures"][0]

        self.assertTrue(entry["privacy_review"]["redaction_confirmed"])
        self.assertEqual(persisted["privacy_review"]["note"], "UID masked")


if __name__ == "__main__":
    unittest.main()
