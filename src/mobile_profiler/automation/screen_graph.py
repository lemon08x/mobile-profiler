"""Typed deterministic screen graph built on the automation skill boundary."""

from __future__ import annotations

import json
import math
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from .contracts import (
    ActionRequest,
    ActionResult,
    ActionRisk,
    ActionStatus,
    AutomationEvent,
    ComponentDescriptor,
    Observation,
    ObservationRequest,
    SkillRequest,
    SkillResult,
)
from .image_matching import (
    NormalizedRegion,
    OpenCvTemplateMatcher,
    TemplateMatch,
    TemplateSpec,
)
from .ports import SkillRuntime


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _name(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not _NAME_RE.fullmatch(normalized):
        raise ValueError(f"{label} is not a valid component name: {value!r}")
    return normalized


def _mapping(value: object, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return {str(key): item for key, item in value.items()}


def _sequence(value: object, label: str) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be an array")
    return tuple(value)


@dataclass(frozen=True)
class VisualState:
    state_id: str
    template_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_id", _name(self.state_id, "state_id"))
        object.__setattr__(self, "template_id", _name(self.template_id, "template_id"))


@dataclass(frozen=True)
class VisualTransition:
    source: str
    target: str
    action_name: str
    action_arguments: Mapping[str, Any] = field(default_factory=dict)
    risk: ActionRisk = ActionRisk.LOW
    settle_s: float = 0.5

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _name(self.source, "transition source"))
        object.__setattr__(self, "target", _name(self.target, "transition target"))
        object.__setattr__(self, "action_name", _name(self.action_name, "action name"))
        object.__setattr__(self, "action_arguments", _mapping(self.action_arguments, "action arguments"))
        object.__setattr__(self, "risk", ActionRisk(self.risk))
        settle_s = float(self.settle_s)
        if not math.isfinite(settle_s) or settle_s < 0 or settle_s > 30:
            raise ValueError("transition settle_s must be within 0..30 seconds")
        object.__setattr__(self, "settle_s", settle_s)


@dataclass(frozen=True)
class VisualScreenGraph:
    graph_id: str
    states: tuple[VisualState, ...]
    transitions: tuple[VisualTransition, ...]
    max_transitions: int = 30

    def __post_init__(self) -> None:
        object.__setattr__(self, "graph_id", _name(self.graph_id, "graph_id"))
        object.__setattr__(self, "states", tuple(self.states))
        object.__setattr__(self, "transitions", tuple(self.transitions))
        if not self.states:
            raise ValueError("screen graph requires at least one state")
        state_ids = [state.state_id for state in self.states]
        if len(state_ids) != len(set(state_ids)):
            raise ValueError("screen graph state IDs must be unique")
        known = set(state_ids)
        for transition in self.transitions:
            if transition.source not in known or transition.target not in known:
                raise ValueError(
                    f"transition references an unknown state: {transition.source} -> {transition.target}"
                )
        if self.max_transitions <= 0:
            raise ValueError("max_transitions must be positive")

    def state(self, state_id: str) -> VisualState:
        normalized = _name(state_id, "state_id")
        for state in self.states:
            if state.state_id == normalized:
                return state
        raise KeyError(normalized)

    def outgoing(self, state_id: str) -> tuple[VisualTransition, ...]:
        normalized = _name(state_id, "state_id")
        return tuple(
            transition for transition in self.transitions if transition.source == normalized
        )

    def shortest_path(self, source: str, target: str) -> tuple[VisualTransition, ...]:
        source = self.state(source).state_id
        target = self.state(target).state_id
        if source == target:
            return ()
        queue: deque[str] = deque([source])
        previous: dict[str, tuple[str, VisualTransition]] = {}
        visited = {source}
        while queue:
            current = queue.popleft()
            for transition in self.outgoing(current):
                if transition.target in visited:
                    continue
                visited.add(transition.target)
                previous[transition.target] = (current, transition)
                if transition.target == target:
                    queue.clear()
                    break
                queue.append(transition.target)
        if target not in previous:
            raise ValueError(f"no route from {source} to {target}")
        reversed_path: list[VisualTransition] = []
        cursor = target
        while cursor != source:
            parent, transition = previous[cursor]
            reversed_path.append(transition)
            cursor = parent
        return tuple(reversed(reversed_path))


@dataclass(frozen=True)
class StateDetection:
    state_id: str
    template_id: str
    observation_revision: str
    match: TemplateMatch


class StateDetector(Protocol):
    def detect(self, observation: Observation) -> Optional[StateDetection]:
        ...


class TemplateStateDetector:
    """Recognize one graph state by selecting the strongest passing template."""

    def __init__(
        self,
        graph: VisualScreenGraph,
        templates: Mapping[str, TemplateSpec],
        *,
        matcher: Optional[OpenCvTemplateMatcher] = None,
    ) -> None:
        self.graph = graph
        self.templates = dict(templates)
        missing = sorted(
            {state.template_id for state in graph.states} - set(self.templates)
        )
        if missing:
            raise ValueError(f"screen graph templates are missing: {', '.join(missing)}")
        self.matcher = matcher or OpenCvTemplateMatcher()

    def detect(self, observation: Observation) -> Optional[StateDetection]:
        if observation.screen is None:
            return None
        best: Optional[StateDetection] = None
        for state in self.graph.states:
            match = self.matcher.match(observation.screen, self.templates[state.template_id])
            if not match.matched:
                continue
            detection = StateDetection(
                state_id=state.state_id,
                template_id=state.template_id,
                observation_revision=observation.revision,
                match=match,
            )
            if best is None or detection.match.score > best.match.score:
                best = detection
        return best


@dataclass(frozen=True)
class VisualAutomationBundle:
    graph: VisualScreenGraph
    templates: tuple[TemplateSpec, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "templates", tuple(self.templates))
        template_ids = [template.template_id for template in self.templates]
        if len(template_ids) != len(set(template_ids)):
            raise ValueError("template IDs must be unique within a bundle")
        missing = sorted(
            {state.template_id for state in self.graph.states} - set(template_ids)
        )
        if missing:
            raise ValueError(f"bundle is missing templates: {', '.join(missing)}")

    @property
    def template_map(self) -> dict[str, TemplateSpec]:
        return {template.template_id: template for template in self.templates}


def load_visual_automation_bundle(path: Path) -> VisualAutomationBundle:
    """Load a JSON resource pack without evaluating executable strings."""

    source = path.resolve()
    data = json.loads(source.read_text(encoding="utf-8"))
    root = _mapping(data, "visual automation bundle")
    templates: list[TemplateSpec] = []
    for index, raw in enumerate(_sequence(root.get("templates"), "templates"), 1):
        item = _mapping(raw, f"template {index}")
        template_path = str(item.get("path") or "").strip()
        if template_path:
            candidate = Path(template_path)
            if not candidate.is_absolute():
                candidate = source.parent / candidate
            template_path = str(candidate.resolve())
        region_value = item.get("region", (0.0, 0.0, 1.0, 1.0))
        region_parts = _sequence(region_value, f"template {index} region")
        if len(region_parts) != 4:
            raise ValueError(f"template {index} region must contain four values")
        templates.append(
            TemplateSpec(
                template_id=_name(item.get("id"), f"template {index} id"),
                template_path=template_path,
                threshold=float(item.get("threshold", 0.90)),
                region=NormalizedRegion(*(float(value) for value in region_parts)),
                scales=tuple(
                    float(value)
                    for value in _sequence(item.get("scales", (1.0,)), f"template {index} scales")
                ),
                grayscale=bool(item.get("grayscale", True)),
            )
        )

    states: list[VisualState] = []
    for index, raw in enumerate(_sequence(root.get("states"), "states"), 1):
        item = _mapping(raw, f"state {index}")
        states.append(
            VisualState(
                state_id=_name(item.get("id"), f"state {index} id"),
                template_id=_name(item.get("template"), f"state {index} template"),
            )
        )

    transitions: list[VisualTransition] = []
    for index, raw in enumerate(_sequence(root.get("transitions", ()), "transitions"), 1):
        item = _mapping(raw, f"transition {index}")
        action = _mapping(item.get("action"), f"transition {index} action")
        transitions.append(
            VisualTransition(
                source=_name(item.get("from"), f"transition {index} source"),
                target=_name(item.get("to"), f"transition {index} target"),
                action_name=_name(action.get("name"), f"transition {index} action name"),
                action_arguments=_mapping(
                    action.get("arguments"), f"transition {index} action arguments"
                ),
                risk=ActionRisk(str(action.get("risk") or ActionRisk.LOW.value)),
                settle_s=float(item.get("settle_s", 0.5)),
            )
        )
    graph = VisualScreenGraph(
        graph_id=_name(root.get("graph_id"), "graph_id"),
        states=tuple(states),
        transitions=tuple(transitions),
        max_transitions=int(root.get("max_transitions", 30)),
    )
    return VisualAutomationBundle(graph, tuple(templates))


class ScreenGraphSkill:
    """Navigate a recognized screen graph through typed host actions."""

    def __init__(
        self,
        graph: VisualScreenGraph,
        detector: StateDetector,
        *,
        name: str = "screen_graph",
        sleep_func=time.sleep,
    ) -> None:
        self.graph = graph
        self.detector = detector
        self.sleep_func = sleep_func
        self._descriptor = ComponentDescriptor(
            name=name,
            version="1",
            description="Bounded deterministic visual screen-graph navigation",
            capabilities=frozenset({"screenshot", "typed_actions", "bounded_navigation"}),
        )

    @property
    def descriptor(self) -> ComponentDescriptor:
        return self._descriptor

    @staticmethod
    def _result(
        request: SkillRequest,
        succeeded: bool,
        message: str,
        *,
        action_results: tuple[ActionResult, ...] = (),
        states: tuple[str, ...] = (),
        target_state: str = "",
    ) -> SkillResult:
        return SkillResult(
            request_id=request.request_id,
            name=request.name,
            succeeded=succeeded,
            message=message,
            action_results=action_results,
            metadata={
                "observed_states": list(states),
                "target_state": target_state,
                "transition_count": len(action_results),
            },
        )

    def execute(self, request: SkillRequest, runtime: SkillRuntime) -> SkillResult:
        target_state = str(request.arguments.get("target_state") or "").strip()
        try:
            self.graph.state(target_state)
        except (KeyError, ValueError):
            return self._result(
                request,
                False,
                f"unknown target state: {target_state or '<empty>'}",
                target_state=target_state,
            )
        try:
            requested_limit = int(
                request.arguments.get("max_transitions", self.graph.max_transitions)
            )
        except (TypeError, ValueError):
            return self._result(
                request,
                False,
                "max_transitions must be an integer",
                target_state=target_state,
            )
        transition_limit = max(1, min(self.graph.max_transitions, requested_limit))
        action_results: list[ActionResult] = []
        observed_states: list[str] = []
        deadline = time.monotonic() + request.timeout_s

        for transition_index in range(transition_limit + 1):
            if time.monotonic() >= deadline:
                return self._result(
                    request,
                    False,
                    "screen graph exceeded the skill timeout",
                    action_results=tuple(action_results),
                    states=tuple(observed_states),
                    target_state=target_state,
                )
            if runtime.stop_requested():
                return self._result(
                    request,
                    False,
                    "screen graph stopped by the host",
                    action_results=tuple(action_results),
                    states=tuple(observed_states),
                    target_state=target_state,
                )
            try:
                observation = runtime.observe(
                    ObservationRequest(
                        channels=frozenset({"screenshot"}),
                        timeout_s=min(max(0.1, deadline - time.monotonic()), 30.0),
                        metadata={"graph_id": self.graph.graph_id},
                    )
                )
                detection = self.detector.detect(observation)
            except Exception as exc:
                return self._result(
                    request,
                    False,
                    f"visual observation failed: {exc}",
                    action_results=tuple(action_results),
                    states=tuple(observed_states),
                    target_state=target_state,
                )
            if detection is None:
                return self._result(
                    request,
                    False,
                    "unable to identify the current visual state",
                    action_results=tuple(action_results),
                    states=tuple(observed_states),
                    target_state=target_state,
                )
            observed_states.append(detection.state_id)
            runtime.emit(
                AutomationEvent(
                    event_type="visual_state_detected",
                    run_id=request.request_id,
                    payload={
                        "graph_id": self.graph.graph_id,
                        "state_id": detection.state_id,
                        "template_id": detection.template_id,
                        "score": detection.match.score,
                        "threshold": detection.match.threshold,
                        "observation_revision": observation.revision,
                    },
                )
            )
            if detection.state_id == target_state:
                return self._result(
                    request,
                    True,
                    f"reached visual state {target_state}",
                    action_results=tuple(action_results),
                    states=tuple(observed_states),
                    target_state=target_state,
                )
            if transition_index >= transition_limit:
                break
            try:
                transition = self.graph.shortest_path(
                    detection.state_id, target_state
                )[0]
            except (ValueError, IndexError) as exc:
                return self._result(
                    request,
                    False,
                    str(exc),
                    action_results=tuple(action_results),
                    states=tuple(observed_states),
                    target_state=target_state,
                )
            try:
                action = ActionRequest(
                    name=transition.action_name,
                    arguments=transition.action_arguments,
                    observation_revision=observation.revision,
                    risk=transition.risk,
                    source=f"skill:{self.descriptor.name}",
                )
            except (TypeError, ValueError) as exc:
                return self._result(
                    request,
                    False,
                    f"invalid transition action: {exc}",
                    action_results=tuple(action_results),
                    states=tuple(observed_states),
                    target_state=target_state,
                )
            if not runtime.profile.allows(action):
                return self._result(
                    request,
                    False,
                    f"task profile disallows action: {action.name}",
                    action_results=tuple(action_results),
                    states=tuple(observed_states),
                    target_state=target_state,
                )
            try:
                action_result = runtime.act(action)
            except Exception as exc:
                return self._result(
                    request,
                    False,
                    f"transition action raised an error: {exc}",
                    action_results=tuple(action_results),
                    states=tuple(observed_states),
                    target_state=target_state,
                )
            action_results.append(action_result)
            runtime.emit(
                AutomationEvent(
                    event_type="visual_transition_executed",
                    run_id=request.request_id,
                    payload={
                        "graph_id": self.graph.graph_id,
                        "source": transition.source,
                        "target": transition.target,
                        "action": transition.action_name,
                        "status": action_result.status.value,
                    },
                )
            )
            if action_result.status != ActionStatus.SUCCEEDED:
                return self._result(
                    request,
                    False,
                    f"transition action failed: {action_result.message or action_result.name}",
                    action_results=tuple(action_results),
                    states=tuple(observed_states),
                    target_state=target_state,
                )
            if transition.settle_s:
                self.sleep_func(transition.settle_s)

        return self._result(
            request,
            False,
            f"transition limit reached before {target_state}",
            action_results=tuple(action_results),
            states=tuple(observed_states),
            target_state=target_state,
        )
