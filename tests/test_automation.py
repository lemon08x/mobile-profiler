from __future__ import annotations

import threading
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
    ObservationRequest,
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
    Uiautomator2Provider,
    ValidationSeverity,
    VerificationMode,
    VerificationPlan,
    VerificationRequest,
    format_ui_hierarchy,
    parse_uiautomator2_hierarchy,
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


class Uiautomator2ProviderTests(unittest.TestCase):
    XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.example.game" content-desc="" clickable="false" enabled="true" bounds="[0,0][1080,2400]">
    <node index="0" text="Play" resource-id="com.example.game:id/play" class="android.widget.Button" package="com.example.game" content-desc="Start game" clickable="true" enabled="true" focusable="true" bounds="[120,1600][960,1780]" />
    <node index="1" text="Score 8" resource-id="com.example.game:id/score" class="android.widget.TextView" package="com.example.game" content-desc="" clickable="false" enabled="true" bounds="[300,180][780,300]" />
  </node>
</hierarchy>"""

    def test_parser_compacts_revision_bound_elements_and_prompt_text(self) -> None:
        hierarchy = parse_uiautomator2_hierarchy(
            self.XML,
            revision="u2-000001-test",
            width=1080,
            height=2400,
        )
        self.assertEqual([item.element_id for item in hierarchy.elements], ["e001", "e002"])
        self.assertEqual(hierarchy.elements[0].text, "Play")
        self.assertTrue(hierarchy.elements[0].clickable)
        observed = Observation(
            revision=hierarchy.revision,
            captured_at=1.0,
            channels=frozenset({"ui_hierarchy"}),
            context=DeviceContext(
                device_id="serial-1",
                foreground_package="com.example.game",
                foreground_activity=".MainActivity",
            ),
            ui=hierarchy,
        )
        prompt = format_ui_hierarchy(observed)
        self.assertIn("revision=u2-000001-test", prompt)
        self.assertIn('[e001]', prompt)
        self.assertIn('text="Play"', prompt)
        self.assertIn("flags=click,focus", prompt)

    def test_parser_marks_large_unlabelled_render_nodes_as_canvas(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.example.game" content-desc="" clickable="false" enabled="true" bounds="[0,0][1080,2400]">
    <node index="0" text="" resource-id="" class="android.view.View" package="com.example.game" content-desc="" clickable="false" enabled="true" bounds="[0,240][1080,2200]" />
  </node>
</hierarchy>"""
        hierarchy = parse_uiautomator2_hierarchy(
            xml,
            revision="u2-000001-canvas",
            width=1080,
            height=2400,
        )
        self.assertEqual(len(hierarchy.elements), 1)
        self.assertTrue(hierarchy.elements[0].attributes["canvas_like"])
        observed = Observation(
            revision=hierarchy.revision,
            captured_at=1.0,
            channels=frozenset({"ui_hierarchy"}),
            context=DeviceContext(device_id="serial-1"),
            ui=hierarchy,
        )
        prompt = format_ui_hierarchy(observed)
        self.assertIn("flags=canvas,non-actionable", prompt)
        self.assertIn("[non-actionable-canvas-1]", prompt)
        self.assertNotIn("[e001]", prompt)

    def test_provider_observes_and_executes_revision_bound_element_click(self) -> None:
        class FakeJsonRpc:
            def __init__(self) -> None:
                self.configurator: dict[str, object] = {}

            def setConfigurator(self, value: dict[str, object]) -> None:
                self.configurator = dict(value)

        class FakeDevice:
            def __init__(self) -> None:
                self.clicks: list[tuple[int, int]] = []
                self.dump_kwargs: dict[str, object] = {}
                self.info = {
                    "screenOn": True,
                    "displayRotation": 0,
                    "currentPackageName": "com.example.game",
                }
                self.settings: dict[str, object] = {}
                self.jsonrpc = FakeJsonRpc()

            def window_size(self) -> tuple[int, int]:
                return 1080, 2400

            def dump_hierarchy(self, **kwargs: object) -> str:
                self.dump_kwargs = dict(kwargs)
                return Uiautomator2ProviderTests.XML

            def app_current(self) -> dict[str, object]:
                raise AssertionError("observe must not call the slow app_current RPC")

            def click(self, x: int, y: int) -> None:
                self.clicks.append((x, y))

        device = FakeDevice()
        provider = Uiautomator2Provider(
            "serial-1",
            connect_factory=lambda _serial: device,
        )
        observed = provider.observe(
            ObservationRequest(channels=frozenset({"ui_hierarchy"}))
        )
        self.assertEqual(observed.context.foreground_package, "com.example.game")
        self.assertTrue(device.dump_kwargs["root_in_active"])
        self.assertEqual(device.dump_kwargs["max_depth"], 40)
        self.assertEqual(device.settings["wait_timeout"], 2.0)
        self.assertEqual(device.jsonrpc.configurator["waitForIdleTimeout"], 500)
        summary = provider.execute_action(
            {
                "action": "tap_element",
                "element_id": "e001",
                "observation_revision": observed.revision,
            },
            observed,
            threading.Event(),
        )
        self.assertEqual(device.clicks, [(540, 1690)])
        self.assertIn("e001", summary)
        with self.assertRaisesRegex(ValueError, "stale"):
            provider.execute_action(
                {
                    "action": "tap_element",
                    "element_id": "e001",
                    "observation_revision": "u2-old",
                },
                observed,
                threading.Event(),
            )

    def test_provider_inputs_text_through_focused_element_without_lazy_ime_install(self) -> None:
        class FakeFocusedElement:
            exists = True

            def __init__(self) -> None:
                self.values: list[str] = []

            def set_text(self, value: str) -> None:
                self.values.append(value)

        class FakeDevice:
            def __init__(self) -> None:
                self.focused = FakeFocusedElement()
                self.send_keys_called = False

            def __call__(self, **selector: object) -> FakeFocusedElement:
                self.selector = selector
                return self.focused

            def send_keys(self, *_args: object, **_kwargs: object) -> None:
                self.send_keys_called = True
                raise AssertionError("input_text must not trigger uiautomator2 IME installation")

        device = FakeDevice()
        provider = Uiautomator2Provider(
            "serial-1",
            connect_factory=lambda _serial: device,
        )
        observed = Observation(
            revision="u2-000001-test",
            captured_at=1.0,
            channels=frozenset({"ui_hierarchy"}),
            context=DeviceContext(device_id="serial-1"),
        )
        summary = provider.execute_action(
            {"action": "input_text", "text": "https://example.org"},
            observed,
            threading.Event(),
        )
        self.assertEqual(device.selector, {"focused": True})
        self.assertEqual(device.focused.values, ["https://example.org"])
        self.assertFalse(device.send_keys_called)
        self.assertIn("https://example.org", summary)

    def test_provider_rejects_tapping_a_non_actionable_canvas_container(self) -> None:
        class FakeDevice:
            def click(self, _x: int, _y: int) -> None:
                raise AssertionError("a canvas container must not be clicked")

        hierarchy = parse_uiautomator2_hierarchy(
            """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="com.example:id/result_list" class="android.widget.FrameLayout" package="com.example" content-desc="" clickable="false" enabled="true" bounds="[0,0][1080,2400]" />
</hierarchy>""",
            revision="u2-000001-canvas",
            width=1080,
            height=2400,
        )
        observed = Observation(
            revision=hierarchy.revision,
            captured_at=1.0,
            channels=frozenset({"ui_hierarchy"}),
            context=DeviceContext(device_id="serial-1"),
            ui=hierarchy,
        )
        provider = Uiautomator2Provider(
            "serial-1",
            connect_factory=lambda _serial: FakeDevice(),
        )
        with self.assertRaisesRegex(ValueError, "non-actionable canvas"):
            provider.execute_action(
                {
                    "action": "tap_element",
                    "element_id": "e001",
                    "observation_revision": observed.revision,
                },
                observed,
                threading.Event(),
            )

    def test_provider_hides_and_rejects_unlabelled_layout_containers(self) -> None:
        class FakeDevice:
            def click(self, _x: int, _y: int) -> None:
                raise AssertionError("an unlabelled layout container must not be clicked")

        hierarchy = parse_uiautomator2_hierarchy(
            """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="com.example:id/app_bar" class="android.widget.LinearLayout" package="com.example" content-desc="" clickable="false" enabled="true" bounds="[0,0][1080,700]" />
</hierarchy>""",
            revision="u2-000001-container",
            width=1080,
            height=2400,
        )
        observed = Observation(
            revision=hierarchy.revision,
            captured_at=1.0,
            channels=frozenset({"ui_hierarchy"}),
            context=DeviceContext(device_id="serial-1"),
            ui=hierarchy,
        )
        prompt = format_ui_hierarchy(observed)
        self.assertIn("[non-actionable-container-1]", prompt)
        self.assertNotIn("[e001]", prompt)
        provider = Uiautomator2Provider(
            "serial-1",
            connect_factory=lambda _serial: FakeDevice(),
        )
        with self.assertRaisesRegex(ValueError, "non-actionable container"):
            provider.execute_action(
                {
                    "action": "tap_element",
                    "element_id": "e001",
                    "observation_revision": observed.revision,
                },
                observed,
                threading.Event(),
            )

    def test_provider_hides_and_rejects_non_clickable_text_labels(self) -> None:
        class FakeDevice:
            def click(self, _x: int, _y: int) -> None:
                raise AssertionError("a non-clickable label must not be clicked")

        hierarchy = parse_uiautomator2_hierarchy(
            """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node index="0" text="Previous app title" resource-id="com.store:id/detail_title" class="android.widget.TextView" package="com.store" content-desc="" clickable="false" enabled="true" bounds="[200,200][880,340]" />
</hierarchy>""",
            revision="u2-000001-label",
            width=1080,
            height=2400,
        )
        observed = Observation(
            revision=hierarchy.revision,
            captured_at=1.0,
            channels=frozenset({"ui_hierarchy"}),
            context=DeviceContext(device_id="serial-1"),
            ui=hierarchy,
        )
        prompt = format_ui_hierarchy(observed)
        self.assertIn("[non-actionable-element-1]", prompt)
        self.assertNotIn("[e001]", prompt)
        provider = Uiautomator2Provider(
            "serial-1",
            connect_factory=lambda _serial: FakeDevice(),
        )
        with self.assertRaisesRegex(ValueError, "non-actionable element"):
            provider.execute_action(
                {
                    "action": "tap_element",
                    "element_id": "e001",
                    "observation_revision": observed.revision,
                },
                observed,
                threading.Event(),
            )

    def test_provider_rejects_semantic_and_coordinate_taps_in_recommendations(self) -> None:
        class FakeDevice:
            def click(self, _x: int, _y: int) -> None:
                raise AssertionError("a recommendation control must not be clicked")

        hierarchy = parse_uiautomator2_hierarchy(
            """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="com.store:id/recommend_download_list_layout" class="android.widget.LinearLayout" package="com.store" content-desc="" clickable="false" enabled="true" bounds="[0,1000][1080,2200]">
    <node index="0" text="Install" resource-id="com.store:id/download_status" class="android.widget.TextView" package="com.store" content-desc="" clickable="true" enabled="true" focusable="true" bounds="[800,1500][1040,1650]" />
  </node>
</hierarchy>""",
            revision="u2-000001-recommendation",
            width=1080,
            height=2400,
        )
        recommendation = hierarchy.elements[1]
        self.assertIn(
            "com.store:id/recommend_download_list_layout",
            recommendation.attributes["ancestor_resource_ids"],
        )
        observed = Observation(
            revision=hierarchy.revision,
            captured_at=1.0,
            channels=frozenset({"ui_hierarchy"}),
            context=DeviceContext(device_id="serial-1"),
            ui=hierarchy,
        )
        provider = Uiautomator2Provider(
            "serial-1",
            connect_factory=lambda _serial: FakeDevice(),
        )
        with self.assertRaisesRegex(ValueError, "app-store recommendation area"):
            provider.execute_action(
                {
                    "action": "tap_element",
                    "element_id": recommendation.element_id,
                    "observation_revision": observed.revision,
                    "_forbid_recommendation_controls": True,
                },
                observed,
                threading.Event(),
            )
        with self.assertRaisesRegex(ValueError, "coordinate target belongs"):
            provider.execute_action(
                {
                    "action": "tap",
                    "element": [850, 655],
                    "_forbid_recommendation_controls": True,
                },
                observed,
                threading.Event(),
            )

    def test_coordinate_policy_allows_specific_primary_control_over_recommendation_background(self) -> None:
        class FakeDevice:
            def __init__(self) -> None:
                self.clicks: list[tuple[int, int]] = []

            def click(self, x: int, y: int) -> None:
                self.clicks.append((x, y))

        hierarchy = parse_uiautomator2_hierarchy(
            """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="com.store:id/recommend_download_list_layout" class="android.widget.LinearLayout" package="com.store" content-desc="" clickable="false" enabled="true" bounds="[0,700][1080,2400]">
    <node index="0" text="Suggested app" resource-id="com.store:id/app_title" class="android.widget.TextView" package="com.store" content-desc="" clickable="false" enabled="true" bounds="[100,900][500,1000]" />
  </node>
  <node index="1" text="" resource-id="com.store:id/download_area" class="android.widget.FrameLayout" package="com.store" content-desc="Install" clickable="true" enabled="true" focusable="true" bounds="[100,2150][980,2350]" />
</hierarchy>""",
            revision="u2-000001-primary-overlap",
            width=1080,
            height=2400,
        )
        observed = Observation(
            revision=hierarchy.revision,
            captured_at=1.0,
            channels=frozenset({"ui_hierarchy"}),
            context=DeviceContext(device_id="serial-1"),
            ui=hierarchy,
        )
        device = FakeDevice()
        provider = Uiautomator2Provider(
            "serial-1",
            connect_factory=lambda _serial: device,
        )
        provider.execute_action(
            {
                "action": "tap",
                "element": [500, 936],
                "_forbid_recommendation_controls": True,
            },
            observed,
            threading.Event(),
        )
        self.assertEqual(len(device.clicks), 1)

    def test_provider_blocks_allow_on_notification_permission_when_policy_requires_deny(self) -> None:
        class FakeDevice:
            def __init__(self) -> None:
                self.clicks: list[tuple[int, int]] = []

            def click(self, x: int, y: int) -> None:
                self.clicks.append((x, y))

        hierarchy = parse_uiautomator2_hierarchy(
            """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node index="0" text="网易云音乐请求向您发送通知" resource-id="android:id/message" class="android.widget.TextView" package="com.android.permissioncontroller" content-desc="" clickable="false" enabled="true" bounds="[100,900][980,1150]" />
  <node index="1" text="允许" resource-id="android:id/button1" class="android.widget.Button" package="com.android.permissioncontroller" content-desc="" clickable="true" enabled="true" focusable="true" bounds="[300,1350][780,1500]" />
  <node index="2" text="禁止" resource-id="android:id/button2" class="android.widget.Button" package="com.android.permissioncontroller" content-desc="" clickable="true" enabled="true" focusable="true" bounds="[300,1650][780,1800]" />
</hierarchy>""",
            revision="u2-000001-notification",
            width=1080,
            height=2400,
        )
        observed = Observation(
            revision=hierarchy.revision,
            captured_at=1.0,
            channels=frozenset({"ui_hierarchy"}),
            context=DeviceContext(device_id="serial-1"),
            ui=hierarchy,
        )
        device = FakeDevice()
        provider = Uiautomator2Provider(
            "serial-1",
            connect_factory=lambda _serial: device,
        )
        allow = next(element for element in hierarchy.elements if element.text == "允许")
        deny = next(element for element in hierarchy.elements if element.text == "禁止")
        with self.assertRaisesRegex(ValueError, "notification permission must be denied"):
            provider.execute_action(
                {
                    "action": "tap_element",
                    "element_id": allow.element_id,
                    "observation_revision": observed.revision,
                    "_forbid_notification_allow": True,
                },
                observed,
                threading.Event(),
            )
        with self.assertRaisesRegex(ValueError, "notification permission must be denied"):
            provider.execute_action(
                {
                    "action": "tap",
                    "element": [500, 595],
                    "_forbid_notification_allow": True,
                },
                observed,
                threading.Event(),
            )
        provider.execute_action(
            {
                "action": "tap_element",
                "element_id": deny.element_id,
                "observation_revision": observed.revision,
                "_forbid_notification_allow": True,
            },
            observed,
            threading.Event(),
        )
        self.assertEqual(len(device.clicks), 1)

    def test_provider_rejects_input_text_without_focused_element(self) -> None:
        class FakeFocusedElement:
            exists = False

        class FakeDevice:
            def __call__(self, **_selector: object) -> FakeFocusedElement:
                return FakeFocusedElement()

        provider = Uiautomator2Provider(
            "serial-1",
            connect_factory=lambda _serial: FakeDevice(),
        )
        observed = Observation(
            revision="u2-000001-test",
            captured_at=1.0,
            channels=frozenset({"ui_hierarchy"}),
            context=DeviceContext(device_id="serial-1"),
        )
        with self.assertRaisesRegex(ValueError, "focused editable element"):
            provider.execute_action(
                {"action": "input_text", "text": "hello"},
                observed,
                threading.Event(),
            )

    def test_provider_retries_input_text_once_after_uiautomator_recovery(self) -> None:
        class FakeFocusedElement:
            exists = True

            def __init__(self, failure: Exception | None = None) -> None:
                self.failure = failure
                self.values: list[str] = []

            def set_text(self, value: str) -> None:
                if self.failure is not None:
                    failure, self.failure = self.failure, None
                    raise failure
                self.values.append(value)

        class FakeDevice:
            def __init__(self, focused: FakeFocusedElement) -> None:
                self.focused = focused

            def __call__(self, **_selector: object) -> FakeFocusedElement:
                return self.focused

        first = FakeDevice(FakeFocusedElement(RuntimeError()))
        recovered = FakeDevice(FakeFocusedElement())
        devices = iter([first, recovered])
        provider = Uiautomator2Provider(
            "serial-1",
            connect_factory=lambda _serial: next(devices),
        )
        observed = Observation(
            revision="u2-000001-test",
            captured_at=1.0,
            channels=frozenset({"ui_hierarchy"}),
            context=DeviceContext(device_id="serial-1"),
        )

        summary = provider.execute_action(
            {"action": "input_text", "text": "淘宝"},
            observed,
            threading.Event(),
        )

        self.assertEqual(recovered.focused.values, ["淘宝"])
        self.assertIn("淘宝", summary)

    def test_provider_rejects_launching_a_missing_package(self) -> None:
        class FakeDevice:
            def __init__(self) -> None:
                self.started = False

            def app_info(self, _package: str) -> dict[str, object]:
                raise RuntimeError("package not found")

            def app_start(self, *_args: object, **_kwargs: object) -> None:
                self.started = True

        device = FakeDevice()
        provider = Uiautomator2Provider(
            "serial-1",
            connect_factory=lambda _serial: device,
        )
        observed = Observation(
            revision="u2-000001-test",
            captured_at=1.0,
            channels=frozenset({"ui_hierarchy"}),
            context=DeviceContext(device_id="serial-1"),
        )
        with self.assertRaisesRegex(ValueError, "package is not installed"):
            provider.execute_action(
                {"action": "launch_app", "package": "com.vivo.appstore"},
                observed,
                threading.Event(),
            )
        self.assertFalse(device.started)

    def test_provider_preserves_empty_third_party_exception_type_after_recovery(self) -> None:
        class FakeDevice:
            def app_info(self, _package: str) -> dict[str, object]:
                return {"packageName": "com.example.app"}

            def app_start(self, *_args: object, **_kwargs: object) -> None:
                raise RuntimeError()

        provider = Uiautomator2Provider(
            "serial-1",
            connect_factory=lambda _serial: FakeDevice(),
        )
        observed = Observation(
            revision="u2-000001-test",
            captured_at=1.0,
            channels=frozenset({"ui_hierarchy"}),
            context=DeviceContext(device_id="serial-1"),
        )
        with self.assertRaisesRegex(RuntimeError, "RuntimeError"):
            provider.execute_action(
                {"action": "launch_app", "package": "com.example.app"},
                observed,
                threading.Event(),
            )

    def test_provider_rejects_task_forbidden_launch_package(self) -> None:
        class FakeDevice:
            def app_info(self, _package: str) -> dict[str, object]:
                raise AssertionError("forbidden package must be rejected before lookup")

        provider = Uiautomator2Provider(
            "serial-1",
            connect_factory=lambda _serial: FakeDevice(),
        )
        observed = Observation(
            revision="u2-000001-test",
            captured_at=1.0,
            channels=frozenset({"ui_hierarchy"}),
            context=DeviceContext(device_id="serial-1"),
        )
        with self.assertRaisesRegex(ValueError, "forbidden for this task"):
            provider.execute_action(
                {
                    "action": "launch_app",
                    "package": "com.bbk.appstore",
                    "_forbid_launch_packages": ("com.bbk.appstore",),
                },
                observed,
                threading.Event(),
            )


if __name__ == "__main__":
    unittest.main()
