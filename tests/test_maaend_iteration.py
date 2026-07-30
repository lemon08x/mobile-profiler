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
    @staticmethod
    def _write_sample_hold_audit_sources(
        root: Path,
    ) -> tuple[Path, Path, Path, Path, Path]:
        adb_input = (
            root
            / "agent/cpp-algo/source/MapNavigator/Backend/Adb/adb_input_backend.cpp"
        )
        navi_config = root / "agent/cpp-algo/source/MapNavigator/navi_config.h"
        navigation = (
            root
            / "agent/cpp-algo/source/MapNavigator/navigation_state_machine.cpp"
        )
        adb_camera = (
            root
            / "agent/cpp-algo/source/MapNavigator/Backend/Adb/adb_camera_swipe_driver.cpp"
        )
        cpp_viewport = root / "agent/cpp-algo/source/Viewport/ViewportSession.cpp"
        action_wrapper = (
            root / "agent/cpp-algo/source/MapNavigator/action_wrapper.cpp"
        )
        for path in (
            adb_input,
            adb_camera,
            cpp_viewport,
            action_wrapper,
            navi_config,
            navigation,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
        adb_input.write_text(
            "AdbCameraSwipeDriverConfig MakeDefaultCameraSwipeDriverConfig() {\n"
            "  config.contact_id = 0;\n"
            "}\n"
            "AdbVirtualJoystickDriverConfig MakeDefaultJoystickDriverConfig() {}\n"
            ".max_batch_delta_deg = 45.0,\n",
            encoding="utf-8",
        )
        adb_camera.write_text(
            "const int touch_dx = dx;\n"
            'LogInfo << "ADB camera swipe transport.";\n'
            "viewport_->Swipe(frame, start, end, "
            "config_.turn_swipe_duration_ms, config_.contact_id, config_.pressure);\n",
            encoding="utf-8",
        )
        action_wrapper.write_text(
            "bool ActionWrapper::SetViewDeltaPolarity(int polarity) {}\n"
            'LogWarn << "View-delta polarity changed from closed-loop evidence.";\n'
            "backend_->SendViewDeltaSync(dx * view_delta_polarity_, dy);\n",
            encoding="utf-8",
        )
        cpp_viewport.write_text(
            "ViewportSession::Capture\n"
            "ViewportSession::TouchDown\n"
            "ViewportSession::TouchUp\n"
            "const bool selected = true;\n"
            "MaaControllerPostTouchUp(controller_, contact);\n"
            "ViewportSession::Swipe\n"
            "MaaControllerPostSwipeV2(controller_);\n",
            encoding="utf-8",
        )
        navi_config.write_text(
            "constexpr int32_t kAdbSampleHoldHeadingCommitPulseMs = 80;\n"
            "constexpr int32_t kAdbSampleHoldAuthoredRunMaxPulseMs = 100;\n"
            "constexpr double kAdbSampleHoldMotionHeadingMinDistanceM = 0.5;\n"
            "constexpr double kAdbSampleHoldTurnResponseMinDeg = 2.0;\n"
            "constexpr double kAdbSampleHoldPolarityResponseMinDeg = 10.0;\n"
            "constexpr double kAdbSampleHoldPolarityResponseMaxDeg = 100.0;\n"
            "constexpr int32_t kAdbSampleHoldPolarityConfirmResponses = 2;\n"
            "constexpr int32_t kAdbSampleHoldTurnResponseTimeoutMs = 12000;\n"
            "constexpr int32_t kAdbSampleHoldOutlierConfirmFrames = 2;\n"
            "constexpr double kUnstickHeadingCorrectionDeg = 20.0;\n",
            encoding="utf-8",
        )
        navigation.write_text(
            "const bool sample_hold_waiting_for_turn = true;\n"
            "if (turn_wait_ms >= kAdbSampleHoldTurnResponseTimeoutMs) {}\n"
            'FailNavigation("adb_turn_no_response");\n'
            "if (sample_hold_waiting_for_turn && !degraded_fix) {\n"
            "  sample_hold_pulse_ms = kAdbSampleHoldHeadingCommitPulseMs;\n"
            "  NoteSampleAndHoldPulse(sample_hold_pulse_ms);\n"
            "  action_wrapper_->PulseForwardSync(sample_hold_pulse_ms);\n"
            "}\n"
            "const bool turn_responded = directional_turn_response;\n"
            "if (sample_hold_polarity_opposition_count_ >= kAdbSampleHoldPolarityConfirmResponses) {\n"
            "  action_wrapper_->SetViewDeltaPolarity(-previous_polarity);\n"
            "}\n"
            'LogWarn << "ADB view-delta polarity calibrated from motion response.";\n'
            "sample_hold_turn_started_at_ = std::chrono::steady_clock::now();\n"
            'RebaseSampleAndHoldHeading("stable_stationary_outlier");\n'
            'RebaseSampleAndHoldHeading("physical_unstick");\n'
            'LogInfo << "ADB sample-and-hold motion heading.";\n'
            'LogWarn << "ADB sample-and-hold released stale motion heading after stable stationary confirmation.";\n'
            'LogInfo << "Physical unstick rebased ADB heading from displacement.";\n'
            "if (!sample_and_hold && route.valid && session_->HasCurrentWaypoint()) {}\n"
            'LogDebug << "ADB sample-and-hold coarse turn requested.";\n'
            "sample_hold_pulse_ms = std::min(sample_hold_pulse_ms, kAdbSampleHoldAuthoredRunMaxPulseMs);\n"
            'LogInfo << "Physical unstick heading correction.";\n'
            'LogInfo << "Physical unstick resumed the authored ADB route after dislodge.";\n'
            "if (!sample_hold_waiting_for_turn) { ExecutePhysicalUnstick(); }\n"
            "if (!sample_hold_waiting_for_turn) { ExecutePhysicalUnstick(); }\n",
            encoding="utf-8",
        )
        return adb_input, adb_camera, navi_config, navigation, action_wrapper

    def test_framework_audit_fails_closed_on_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = audit_maaframework_source(Path(directory))
        self.assertFalse(result["valid"])
        self.assertGreaterEqual(len(result["failures"]), 5)

    def test_framework_audit_pins_remote_borrowed_handle_lifetime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote_tasker = (
                root
                / "source/MaaAgentServer/RemoteInstance/RemoteTasker.cpp"
            )
            remote_tasker.parent.mkdir(parents=True, exist_ok=True)
            remote_tasker.write_text(
                "if (resource_) {\n        return resource_.get();\n    }\n"
                "if (controller_) {\n        return controller_.get();\n    }\n",
                encoding="utf-8",
            )
            baseline = audit_maaframework_source(root)
            self.assertFalse(
                any("borrowed handles" in item for item in baseline["failures"])
            )

            remote_tasker.write_text(
                "resource_ = std::make_unique<RemoteResource>();\n"
                "controller_ = std::make_unique<RemoteController>();\n",
                encoding="utf-8",
            )
            drift = audit_maaframework_source(root)
            self.assertEqual(
                sum("borrowed handles" in item for item in drift["failures"]),
                2,
            )

    def test_agent_audit_fails_closed_on_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = audit_maaend_agent_source(Path(directory))
        self.assertFalse(result["valid"])
        self.assertGreaterEqual(len(result["failures"]), 2)

    def test_agent_audit_pins_real_device_location_assertion_fast_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (
                root
                / "agent"
                / "cpp-algo"
                / "source"
                / "MapLocator"
                / "MapLocateAction.cpp"
            )
            source.parent.mkdir(parents=True)
            source.write_text(
                "\n".join(
                    (
                        "constexpr int kAssertSettleMaxFrames = 8;",
                        "constexpr int kAssertMaxUnavailableFrames = 3;",
                        "bool IsPositionConclusiveOutsideRect();",
                        "const bool usable = located && !result.position->isHeld;",
                        'LogInfo << "MapLocateAssertLocation quick miss (location unavailable)";',
                    )
                ),
                encoding="utf-8",
            )
            baseline = audit_maaend_agent_source(root)
            relevant = [
                failure
                for failure in baseline["failures"]
                if "location assertion" in failure
                or "minimap frames" in failure
                or "far-outside location" in failure
                or "map positions" in failure
            ]
            self.assertEqual(relevant, [])

            source.write_text(
                source.read_text(encoding="utf-8").replace(
                    "kAssertSettleMaxFrames = 8",
                    "kAssertSettleMaxFrames = 60",
                ),
                encoding="utf-8",
            )
            drift = audit_maaend_agent_source(root)

        self.assertTrue(
            any(
                "location assertion frame budget drifted" in failure
                for failure in drift["failures"]
            )
        )

    def test_agent_audit_pins_sample_hold_camera_and_recovery_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adb_input, adb_camera, _, navigation, action_wrapper = (
                self._write_sample_hold_audit_sources(root)
            )
            baseline = audit_maaend_agent_source(root)
            relevant = [
                failure
                for failure in baseline["failures"]
                if "serialized ADB camera" in failure
                or "camera batch cap" in failure
                or "camera response timeout" in failure
                or "unresponsive ADB camera" in failure
                or "heading commit" in failure
                or "body-heading commit" in failure
                or "body-heading response" in failure
                or "watchdog no longer renews" in failure
                or "physical movement while" in failure
                or "physical-device measurement" in failure
                or "canonical viewport swipe" in failure
                or "measured duration" in failure
                or "calibrated per navigation session" in failure
                or "covers every camera caller" in failure
                or "polarity changes are no longer auditable" in failure
                or "polarity calibration" in failure
            ]
            self.assertEqual(relevant, [])

            adb_input.write_text(
                adb_input.read_text(encoding="utf-8").replace(
                    "config.contact_id = 0;", "config.contact_id = 1;"
                ),
                encoding="utf-8",
            )
            contact_drift = audit_maaend_agent_source(root)
            self.assertTrue(
                any(
                    "serialized ADB camera swipe" in failure
                    for failure in contact_drift["failures"]
                )
            )

            adb_camera.write_text(
                adb_camera.read_text(encoding="utf-8").replace(
                    "viewport_->Swipe(", "ExecuteStableDrag("
                ),
                encoding="utf-8",
            )
            camera_transport_drift = audit_maaend_agent_source(root)
            self.assertTrue(
                any(
                    "canonical viewport swipe contract" in failure
                    for failure in camera_transport_drift["failures"]
                )
            )

            adb_camera.write_text(
                adb_camera.read_text(encoding="utf-8").replace(
                    "const int touch_dx = dx;", "const int touch_dx = -dx;"
                ),
                encoding="utf-8",
            )
            camera_direction_drift = audit_maaend_agent_source(root)
            self.assertTrue(
                any(
                    "inverse touch drag" in failure
                    or "physical-device measurement" in failure
                    for failure in camera_direction_drift["failures"]
                )
            )

            action_wrapper.write_text(
                action_wrapper.read_text(encoding="utf-8").replace(
                    "dx * view_delta_polarity_", "dx"
                ),
                encoding="utf-8",
            )
            polarity_drift = audit_maaend_agent_source(root)
            self.assertTrue(
                any(
                    "covers every camera caller" in failure
                    for failure in polarity_drift["failures"]
                )
            )

            navigation.write_text(
                navigation.read_text(encoding="utf-8").replace(
                    "!sample_hold_waiting_for_turn", "allow_recovery", 1
                ),
                encoding="utf-8",
            )
            recovery_drift = audit_maaend_agent_source(root)
            self.assertTrue(
                any(
                    "physical movement while an ADB camera turn" in failure
                    for failure in recovery_drift["failures"]
                )
            )

            navigation.write_text(
                navigation.read_text(encoding="utf-8").replace(
                    "action_wrapper_->PulseForwardSync(sample_hold_pulse_ms);",
                    "// commit pulse removed",
                ),
                encoding="utf-8",
            )
            commit_drift = audit_maaend_agent_source(root)
            self.assertTrue(
                any(
                    "commits the avatar body heading" in failure
                    for failure in commit_drift["failures"]
                )
            )

            navigation.write_text(
                navigation.read_text(encoding="utf-8").replace(
                    "sample_hold_turn_started_at_ = std::chrono::steady_clock::now();",
                    "// response renewal removed",
                ),
                encoding="utf-8",
            )
            response_drift = audit_maaend_agent_source(root)
            self.assertTrue(
                any(
                    "watchdog no longer renews" in failure
                    for failure in response_drift["failures"]
                )
            )

            navigation.write_text(
                navigation.read_text(encoding="utf-8").replace(
                    'RebaseSampleAndHoldHeading("stable_stationary_outlier");',
                    "// stable outlier recovery removed",
                ),
                encoding="utf-8",
            )
            rebase_drift = audit_maaend_agent_source(root)
            self.assertTrue(
                any(
                    "recover a stale baseline" in failure
                    for failure in rebase_drift["failures"]
                )
            )

            navigation.write_text(
                navigation.read_text(encoding="utf-8").replace(
                    'LogInfo << "Physical unstick resumed the authored ADB route after dislodge.";',
                    "// authored route resume removed",
                ),
                encoding="utf-8",
            )
            authored_route_drift = audit_maaend_agent_source(root)
            self.assertTrue(
                any(
                    "skip authored water-edge route points" in failure
                    for failure in authored_route_drift["failures"]
                )
            )

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
