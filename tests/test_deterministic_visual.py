from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mobile_profiler.automation import (
    ActionResult,
    ActionStatus,
    Artifact,
    Bounds,
    DeviceContext,
    NormalizedRegion,
    Observation,
    ScreenFrame,
    ScreenGraphSkill,
    SkillRequest,
    StateDetection,
    TaskCapabilityProfile,
    TemplateMatch,
    TemplateMatchVerifier,
    TemplateSpec,
    VerificationRequest,
    VerificationStatus,
    VisualScreenGraph,
    VisualState,
    VisualTransition,
    load_visual_automation_bundle,
    template_matching_dependency_status,
)


def screenshot_observation(revision: str) -> Observation:
    return Observation(
        revision=revision,
        captured_at=1.0,
        channels=frozenset({"screenshot"}),
        context=DeviceContext(device_id="serial-1"),
        screen=ScreenFrame(
            Artifact(f"frame-{revision}", "image/png", data=b"synthetic-frame"),
            width=1080,
            height=2400,
        ),
    )


def detected(state_id: str, revision: str, score: float = 0.99) -> StateDetection:
    return StateDetection(
        state_id=state_id,
        template_id=f"template-{state_id}",
        observation_revision=revision,
        match=TemplateMatch(
            template_id=f"template-{state_id}",
            matched=True,
            score=score,
            threshold=0.9,
            bounds=Bounds(100, 200, 300, 400),
            region_bounds=Bounds(0, 0, 1080, 2400),
            scale=1.0,
            elapsed_s=0.01,
            frame_sha256="a" * 64,
        ),
    )


class DeterministicVisualContractTests(unittest.TestCase):
    def test_normalized_region_converts_to_bounded_pixels(self) -> None:
        region = NormalizedRegion(0.25, 0.1, 0.75, 0.9)
        self.assertEqual(region.pixel_bounds(1000, 2000), Bounds(250, 200, 750, 1800))
        with self.assertRaisesRegex(ValueError, "left"):
            NormalizedRegion(0.8, 0.0, 0.2, 1.0)

    def test_screen_graph_finds_shortest_typed_route(self) -> None:
        graph = VisualScreenGraph(
            graph_id="demo",
            states=(
                VisualState("home", "template-home"),
                VisualState("menu", "template-menu"),
                VisualState("game", "template-game"),
            ),
            transitions=(
                VisualTransition("home", "menu", "tap", {"element": [900, 100]}),
                VisualTransition("menu", "game", "tap", {"element": [500, 800]}),
                VisualTransition("home", "game", "launch_app", {"package": "com.demo"}),
            ),
        )
        path = graph.shortest_path("home", "game")
        self.assertEqual(len(path), 1)
        self.assertEqual(path[0].action_name, "launch_app")
        with self.assertRaisesRegex(ValueError, "unknown state"):
            VisualScreenGraph(
                graph_id="broken",
                states=(VisualState("home", "template-home"),),
                transitions=(VisualTransition("home", "missing", "tap"),),
            )

    def test_bundle_loader_resolves_templates_and_rejects_executable_strings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "home.png").write_bytes(b"placeholder")
            config = root / "bundle.json"
            config.write_text(
                json.dumps(
                    {
                        "graph_id": "demo",
                        "max_transitions": 4,
                        "templates": [
                            {
                                "id": "home-template",
                                "path": "home.png",
                                "threshold": 0.91,
                                "region": [0, 0, 1, 1],
                                "scales": [0.9, 1.0, 1.1],
                            }
                        ],
                        "states": [{"id": "home", "template": "home-template"}],
                        "transitions": [],
                    }
                ),
                encoding="utf-8",
            )
            bundle = load_visual_automation_bundle(config)
            self.assertEqual(bundle.graph.graph_id, "demo")
            self.assertEqual(Path(bundle.templates[0].template_path), root / "home.png")

            unsafe = root / "unsafe.json"
            unsafe.write_text(
                json.dumps(
                    {
                        "graph_id": "unsafe",
                        "templates": [
                            {"id": "a-template", "path": "home.png"},
                            {"id": "b-template", "path": "home.png"},
                        ],
                        "states": [
                            {"id": "a", "template": "a-template"},
                            {"id": "b", "template": "b-template"},
                        ],
                        "transitions": [
                            {
                                "from": "a",
                                "to": "b",
                                "action": "eval(\"dangerous()\")",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "action must be an object"):
                load_visual_automation_bundle(unsafe)

    def test_template_verifier_returns_score_and_evidence(self) -> None:
        class FakeMatcher:
            def match(self, screen, spec):
                self.screen = screen
                self.spec = spec
                return TemplateMatch(
                    template_id=spec.template_id,
                    matched=True,
                    score=0.97,
                    threshold=spec.threshold,
                    bounds=Bounds(10, 20, 40, 60),
                    region_bounds=Bounds(0, 0, screen.width, screen.height),
                    scale=1.0,
                    elapsed_s=0.003,
                    frame_sha256=screen.artifact.sha256,
                )

            def annotated_artifact(self, screen, match, *, artifact_id):
                return Artifact(artifact_id, "image/png", data=b"overlay")

        class FakeSink:
            def __init__(self) -> None:
                self.artifacts = []

            def record(self, event) -> None:  # pragma: no cover - protocol completeness
                self.event = event

            def store(self, artifact) -> str:
                self.artifacts.append(artifact)
                return artifact.artifact_id

        sink = FakeSink()
        verifier = TemplateMatchVerifier(
            {"play": TemplateSpec("play", template_data=b"template", threshold=0.9)},
            matcher=FakeMatcher(),  # type: ignore[arg-type]
            evidence_sink=sink,
        )
        result = verifier.verify(
            VerificationRequest("template_match", {"template_id": "play"}),
            screenshot_observation("revision-1"),
        )
        self.assertEqual(result.status, VerificationStatus.PASSED)
        self.assertEqual(result.metadata["score"], 0.97)
        self.assertEqual(result.metadata["bounds"], [10, 20, 40, 60])
        self.assertEqual(len(result.evidence_ids), 1)
        self.assertEqual(len(sink.artifacts), 1)


class ScreenGraphSkillTests(unittest.TestCase):
    def test_skill_detects_routes_and_executes_only_typed_actions(self) -> None:
        graph = VisualScreenGraph(
            graph_id="demo",
            states=(
                VisualState("home", "template-home"),
                VisualState("menu", "template-menu"),
                VisualState("game", "template-game"),
            ),
            transitions=(
                VisualTransition("home", "menu", "tap", {"element": [900, 100]}, settle_s=0),
                VisualTransition("menu", "game", "tap", {"element": [500, 800]}, settle_s=0),
            ),
        )

        class FakeDetector:
            states = {
                "revision-1": detected("home", "revision-1"),
                "revision-2": detected("menu", "revision-2"),
                "revision-3": detected("game", "revision-3"),
            }

            def detect(self, observation):
                return self.states.get(observation.revision)

        class FakeRuntime:
            def __init__(self) -> None:
                self.profile = TaskCapabilityProfile(
                    profile_id="visual-demo",
                    observations=frozenset({"screenshot"}),
                    actions=frozenset({"tap"}),
                    skills=frozenset({"screen_graph"}),
                )
                self.observations = iter(
                    [
                        screenshot_observation("revision-1"),
                        screenshot_observation("revision-2"),
                        screenshot_observation("revision-3"),
                    ]
                )
                self.actions = []
                self.events = []

            def observe(self, request):
                self.request = request
                return next(self.observations)

            def act(self, action):
                self.actions.append(action)
                return ActionResult(
                    action.request_id,
                    action.name,
                    ActionStatus.SUCCEEDED,
                    before_revision=action.observation_revision,
                )

            def verify(self, request):  # pragma: no cover - not used by this skill
                raise AssertionError(request)

            def emit(self, event):
                self.events.append(event)

            def stop_requested(self):
                return False

        runtime = FakeRuntime()
        result = ScreenGraphSkill(
            graph,
            FakeDetector(),
            sleep_func=lambda _seconds: None,
        ).execute(
            SkillRequest("screen_graph", {"target_state": "game"}),
            runtime,
        )
        self.assertTrue(result.succeeded, result.message)
        self.assertEqual(result.metadata["observed_states"], ["home", "menu", "game"])
        self.assertEqual([action.name for action in runtime.actions], ["tap", "tap"])
        self.assertEqual(
            [action.observation_revision for action in runtime.actions],
            ["revision-1", "revision-2"],
        )
        self.assertTrue(all(action.source == "skill:screen_graph" for action in runtime.actions))
        self.assertEqual(len(runtime.events), 5)


@unittest.skipUnless(template_matching_dependency_status()[0], "OpenCV image extra is absent")
class OpenCvTemplateMatcherIntegrationTests(unittest.TestCase):
    def test_real_opencv_match_finds_synthetic_template(self) -> None:
        import cv2
        import numpy

        from mobile_profiler.automation import OpenCvTemplateMatcher

        rng = numpy.random.default_rng(7)
        template = rng.integers(0, 255, size=(60, 90), dtype=numpy.uint8)
        frame = numpy.zeros((600, 400), dtype=numpy.uint8)
        frame[320:380, 170:260] = template
        encoded_frame, frame_payload = cv2.imencode(".png", frame)
        encoded_template, template_payload = cv2.imencode(".png", template)
        self.assertTrue(encoded_frame)
        self.assertTrue(encoded_template)
        screen = ScreenFrame(
            Artifact("opencv-frame", "image/png", data=frame_payload.tobytes()),
            width=400,
            height=600,
        )
        match = OpenCvTemplateMatcher().match(
            screen,
            TemplateSpec(
                "synthetic",
                template_data=template_payload.tobytes(),
                threshold=0.99,
                region=NormalizedRegion(0.2, 0.3, 0.9, 0.9),
                scales=(0.9, 1.0, 1.1),
            ),
        )
        self.assertTrue(match.matched)
        self.assertGreaterEqual(match.score, 0.999)
        self.assertEqual(match.bounds, Bounds(170, 320, 260, 380))

    def test_flat_template_uses_normalized_difference_without_false_positive(self) -> None:
        import cv2
        import numpy

        from mobile_profiler.automation import OpenCvTemplateMatcher

        template = numpy.full((24, 36), 230, dtype=numpy.uint8)
        frame = numpy.zeros((180, 240), dtype=numpy.uint8)
        frame[90:114, 130:166] = template
        encoded_frame, frame_payload = cv2.imencode(".png", frame)
        encoded_template, template_payload = cv2.imencode(".png", template)
        self.assertTrue(encoded_frame)
        self.assertTrue(encoded_template)
        match = OpenCvTemplateMatcher().match(
            ScreenFrame(
                Artifact("flat-frame", "image/png", data=frame_payload.tobytes()),
                width=240,
                height=180,
            ),
            TemplateSpec(
                "flat-template",
                template_data=template_payload.tobytes(),
                threshold=0.99,
            ),
        )
        self.assertTrue(match.matched)
        self.assertEqual(match.bounds, Bounds(130, 90, 166, 114))
