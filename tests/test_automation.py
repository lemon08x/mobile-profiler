from __future__ import annotations

import unittest

from mobile_profiler.automation import (
    SCENARIO_COMPLETE,
    ActionRequest,
    ActionRisk,
    AgentDecision,
    AgentDirectiveKind,
    AgentRequest,
    Artifact,
    Bounds,
    ComponentCatalog,
    ComponentDescriptor,
    ComponentRegistry,
    DeviceCapabilities,
    DeviceContext,
    DeviceGateway,
    DeviceHealth,
    Observation,
    OperationCall,
    OperationKind,
    ScenarioDefinition,
    ScenarioGraphValidator,
    ScenarioNode,
    StateSignaturePolicy,
    TaskCapabilityProfile,
    UiElement,
    UiHierarchy,
    UiStateTracker,
    ValidationSeverity,
    VerificationMode,
    VerificationPlan,
    VerificationRequest,
    ui_state_signature,
)


def observation(
    revision: str,
    *,
    activity: str = ".MainActivity",
    elements: tuple[UiElement, ...] = (),
) -> Observation:
    return Observation(
        revision=revision,
        captured_at=1.0,
        channels=frozenset({"ui_hierarchy", "foreground_activity"}),
        context=DeviceContext(
            device_id="serial-1",
            foreground_package="com.example.app",
            foreground_activity=activity,
        ),
        ui=UiHierarchy(
            revision=revision,
            width=1080,
            height=2400,
            elements=elements,
            source="uiautomator2",
        ),
    )


class AutomationContractTests(unittest.TestCase):
    def test_artifact_digest_and_revision_bound_element_action(self) -> None:
        artifact = Artifact("screen-1", "image/png", data=b"png")
        self.assertEqual(len(artifact.sha256), 64)

        action = ActionRequest.tap_element("e17", "revision-3")
        self.assertEqual(action.name, "tap_element")
        self.assertEqual(action.arguments, {"element_id": "e17"})
        self.assertEqual(action.observation_revision, "revision-3")

        profile = TaskCapabilityProfile(
            profile_id="settings-init",
            observations=frozenset({"screenshot", "ui_hierarchy"}),
            actions=frozenset({"tap_element", "back"}),
        )
        self.assertTrue(profile.allows(action))
        self.assertFalse(
            profile.allows(ActionRequest("factory_reset", risk=ActionRisk.DESTRUCTIVE))
        )
        with self.assertRaisesRegex(ValueError, "observation_revision"):
            ActionRequest("tap_element", {"element_id": "e17"})

    def test_agent_decision_exposes_one_typed_directive(self) -> None:
        profile = TaskCapabilityProfile(
            profile_id="settings-init",
            observations=frozenset({"ui_hierarchy"}),
            actions=frozenset({"back"}),
        )
        request = AgentRequest(
            "Return to the previous screen",
            observation("revision-1"),
            profile,
        )
        decision = AgentDecision(
            request.request_id,
            AgentDirectiveKind.ACTION,
            action=ActionRequest("back"),
        )
        self.assertIsNotNone(decision.action)
        self.assertEqual(decision.action.name, "back")  # type: ignore[union-attr]
        with self.assertRaisesRegex(ValueError, "requires only action"):
            AgentDecision(request.request_id, AgentDirectiveKind.ACTION)

    def test_hierarchy_rejects_duplicate_element_ids(self) -> None:
        element = UiElement("e1", Bounds(0, 0, 100, 100))
        with self.assertRaisesRegex(ValueError, "unique"):
            UiHierarchy("revision-1", 1080, 2400, (element, element), "adb_ui_dump")

    def test_verification_plan_has_explicit_composition_semantics(self) -> None:
        first = VerificationRequest("foreground_package")
        second = VerificationRequest("setting_value")
        plan = VerificationPlan((first, second), VerificationMode.ALL)
        self.assertEqual(plan.mode, VerificationMode.ALL)
        with self.assertRaisesRegex(ValueError, "at least one"):
            VerificationPlan(())
        with self.assertRaisesRegex(ValueError, "unique"):
            VerificationPlan((first, first))

    def test_device_gateway_is_a_structural_port(self) -> None:
        class FakeGateway:
            def capabilities(self) -> DeviceCapabilities:
                return DeviceCapabilities(
                    "serial-1",
                    frozenset({"screenshot"}),
                    frozenset({"back"}),
                )

            def health(self) -> DeviceHealth:
                return DeviceHealth(True)

            def perform(self, action: ActionRequest):  # pragma: no cover - signature check
                raise NotImplementedError

            def recover(self) -> DeviceHealth:
                return DeviceHealth(True)

        self.assertIsInstance(FakeGateway(), DeviceGateway)


class ComponentRegistryTests(unittest.TestCase):
    def test_registry_has_explicit_replace_semantics(self) -> None:
        registry: ComponentRegistry[object] = ComponentRegistry()
        first = object()
        second = object()
        descriptor = ComponentDescriptor("permission_dialog", "1")

        registry.register(descriptor, first)
        self.assertIs(registry.resolve("permission_dialog"), first)
        self.assertEqual(registry.names(), ("permission_dialog",))
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(descriptor, second)

        registry.register(descriptor, second, replace=True)
        self.assertIs(registry.resolve("permission_dialog"), second)
        self.assertIs(registry.unregister("permission_dialog"), second)
        self.assertEqual(len(registry), 0)


class ScenarioGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = ComponentCatalog(
            actions=frozenset({"tap_element", "back"}),
            skills=frozenset({"set_text_utf8", "reset_app"}),
            agents=frozenset({"qwen_step"}),
            verifiers=frozenset({"foreground_package", "setting_value"}),
            watchers=frozenset({"permission_dialog", "app_crash"}),
        )

    def test_valid_graph_supports_setup_cleanup_branching_and_agent_nodes(self) -> None:
        definition = ScenarioDefinition(
            scenario_id="search-flow",
            entry_node="plan",
            setup=(OperationCall(OperationKind.SKILL, "reset_app"),),
            cleanup=(OperationCall(OperationKind.ACTION, "back"),),
            final_verifiers=("foreground_package",),
            nodes=(
                ScenarioNode(
                    "plan",
                    OperationCall(OperationKind.AGENT, "qwen_step"),
                    on_success="input",
                    watchers=("permission_dialog", "app_crash"),
                ),
                ScenarioNode(
                    "input",
                    OperationCall(OperationKind.SKILL, "set_text_utf8"),
                    on_success=SCENARIO_COMPLETE,
                    verifiers=("setting_value",),
                    max_attempts=2,
                ),
            ),
        )

        result = ScenarioGraphValidator().validate(definition, self.catalog)
        self.assertTrue(result.valid, result.issues)
        self.assertEqual(result.issues, ())

    def test_validator_reports_unknown_references_and_unreachable_nodes(self) -> None:
        definition = ScenarioDefinition(
            scenario_id="broken-flow",
            entry_node="start",
            nodes=(
                ScenarioNode(
                    "start",
                    OperationCall(OperationKind.ACTION, "missing_action"),
                    on_success="missing_node",
                    verifiers=("missing_verifier",),
                ),
                ScenarioNode(
                    "unused",
                    OperationCall(OperationKind.ACTION, "back"),
                ),
            ),
        )

        result = ScenarioGraphValidator().validate(definition, self.catalog)
        self.assertFalse(result.valid)
        codes = {issue.code for issue in result.issues}
        self.assertIn("unknown_operation", codes)
        self.assertIn("unknown_verifier", codes)
        self.assertIn("missing_transition", codes)
        self.assertIn("unreachable_node", codes)
        self.assertTrue(
            any(issue.severity == ValidationSeverity.WARNING for issue in result.issues)
        )

    def test_validator_rejects_unbounded_graph_without_terminal_edge(self) -> None:
        definition = ScenarioDefinition(
            scenario_id="loop-only",
            entry_node="a",
            nodes=(
                ScenarioNode(
                    "a",
                    OperationCall(OperationKind.ACTION, "back"),
                    on_success="b",
                    on_failure="b",
                ),
                ScenarioNode(
                    "b",
                    OperationCall(OperationKind.ACTION, "back"),
                    on_success="a",
                    on_failure="a",
                ),
            ),
        )
        result = ScenarioGraphValidator().validate(definition, self.catalog)
        self.assertIn("no_terminal", {issue.code for issue in result.errors})

    def test_validator_can_restrict_graph_to_one_task_capability_profile(self) -> None:
        definition = ScenarioDefinition(
            scenario_id="profile-check",
            entry_node="start",
            nodes=(
                ScenarioNode(
                    "start",
                    OperationCall(OperationKind.ACTION, "tap_element"),
                    verifiers=("setting_value",),
                    watchers=("permission_dialog",),
                ),
            ),
        )
        profile = TaskCapabilityProfile(
            profile_id="read-only",
            observations=frozenset({"screenshot"}),
            actions=frozenset({"back"}),
            verifiers=frozenset({"foreground_package"}),
        )

        result = ScenarioGraphValidator().validate(definition, self.catalog, profile)
        codes = {issue.code for issue in result.errors}
        self.assertIn("profile_disallows_action", codes)
        self.assertIn("profile_disallows_verifier", codes)
        self.assertIn("profile_disallows_watcher", codes)


class UiStateTrackerTests(unittest.TestCase):
    def test_signature_ignores_provider_element_ids_order_and_text_by_default(self) -> None:
        first = observation(
            "revision-1",
            elements=(
                UiElement(
                    "e1",
                    Bounds(0, 100, 500, 200),
                    text="10:31",
                    resource_id="android:id/title",
                    class_name="android.widget.TextView",
                ),
                UiElement(
                    "e2",
                    Bounds(500, 100, 1000, 200),
                    text="Settings",
                    resource_id="android:id/button1",
                    clickable=True,
                ),
            ),
        )
        second = observation(
            "revision-2",
            elements=(
                UiElement(
                    "node-b",
                    Bounds(500, 100, 1000, 200),
                    text="设置",
                    resource_id="android:id/button1",
                    clickable=True,
                ),
                UiElement(
                    "node-a",
                    Bounds(0, 100, 500, 200),
                    text="10:32",
                    resource_id="android:id/title",
                    class_name="android.widget.TextView",
                ),
            ),
        )

        self.assertEqual(ui_state_signature(first).digest, ui_state_signature(second).digest)
        text_policy = StateSignaturePolicy(include_text=True)
        self.assertNotEqual(
            ui_state_signature(first, text_policy).digest,
            ui_state_signature(second, text_policy).digest,
        )

    def test_tracker_detects_two_state_cycle(self) -> None:
        tracker = UiStateTracker()
        states = [
            observation("r1", activity=".A"),
            observation("r2", activity=".B"),
            observation("r3", activity=".A"),
            observation("r4", activity=".B"),
            observation("r5", activity=".A"),
            observation("r6", activity=".B"),
        ]
        for index, state in enumerate(states):
            tracker.add(state, action_name=None if index == 0 else "back")

        detection = tracker.detect_loop(repeat_threshold=3)
        self.assertTrue(detection.detected)
        self.assertEqual(detection.kind, "state_cycle")
        self.assertEqual(detection.cycle_length, 2)
        self.assertEqual(len(tracker.transitions), 5)


if __name__ == "__main__":
    unittest.main()
