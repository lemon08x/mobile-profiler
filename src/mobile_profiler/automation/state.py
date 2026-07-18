"""UI-state signatures, transition history, and bounded loop detection."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional

from .contracts import Observation, UiElement


@dataclass(frozen=True)
class StateSignaturePolicy:
    include_text: bool = False
    include_content_description: bool = True
    include_resource_id: bool = True
    include_class_name: bool = True
    include_bounds: bool = True
    include_screenshot_digest_without_ui: bool = True
    ignored_resource_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class UiStateSignature:
    digest: str
    revision: str
    foreground_package: str
    foreground_activity: str
    element_count: int


@dataclass(frozen=True)
class UiTransition:
    before: str
    action_name: str
    after: str


@dataclass(frozen=True)
class LoopDetection:
    detected: bool
    kind: str = ""
    cycle_length: int = 0
    repetitions: int = 0
    state_digests: tuple[str, ...] = ()


def ui_state_signature(
    observation: Observation,
    policy: StateSignaturePolicy = StateSignaturePolicy(),
) -> UiStateSignature:
    """Create a provider-neutral signature independent of transient element IDs."""

    context = observation.context
    payload: dict[str, object] = {
        "package": context.foreground_package,
        "activity": context.foreground_activity,
        "orientation": context.orientation,
    }
    elements: list[tuple[object, ...]] = []
    if observation.ui is not None:
        for element in observation.ui.elements:
            if not element.visible or element.resource_id in policy.ignored_resource_ids:
                continue
            elements.append(
                _element_signature(
                    element,
                    observation.ui.width,
                    observation.ui.height,
                    policy,
                )
            )
        elements.sort(key=lambda item: json.dumps(item, ensure_ascii=False))
        payload["elements"] = elements
    elif policy.include_screenshot_digest_without_ui and observation.screen is not None:
        payload["screenshot"] = observation.screen.artifact.sha256
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return UiStateSignature(
        hashlib.sha256(canonical).hexdigest(),
        observation.revision,
        context.foreground_package,
        context.foreground_activity,
        len(elements),
    )


def _element_signature(
    element: UiElement,
    width: int,
    height: int,
    policy: StateSignaturePolicy,
) -> tuple[object, ...]:
    values: list[object] = [
        element.package,
        element.clickable,
        element.enabled,
        element.scrollable,
        element.selected,
        element.checked,
    ]
    if policy.include_resource_id:
        values.append(element.resource_id)
    if policy.include_class_name:
        values.append(element.class_name)
    if policy.include_content_description:
        values.append(element.content_description)
    if policy.include_text:
        values.append(element.text)
    if policy.include_bounds:
        values.extend(element.bounds.normalized(width, height))
    return tuple(values)


class UiStateTracker:
    """Records a bounded state sequence and detects repeated states or cycles."""

    def __init__(
        self,
        *,
        policy: StateSignaturePolicy = StateSignaturePolicy(),
        history_limit: int = 100,
    ) -> None:
        if history_limit < 4:
            raise ValueError("history_limit must be at least 4")
        self.policy = policy
        self._states: Deque[UiStateSignature] = deque(maxlen=history_limit)
        self._transitions: Deque[UiTransition] = deque(maxlen=history_limit - 1)

    def add(
        self,
        observation: Observation,
        *,
        action_name: Optional[str] = None,
    ) -> UiStateSignature:
        signature = ui_state_signature(observation, self.policy)
        if action_name is not None and self._states:
            self._transitions.append(
                UiTransition(self._states[-1].digest, str(action_name), signature.digest)
            )
        self._states.append(signature)
        return signature

    @property
    def states(self) -> tuple[UiStateSignature, ...]:
        return tuple(self._states)

    @property
    def transitions(self) -> tuple[UiTransition, ...]:
        return tuple(self._transitions)

    def detect_loop(
        self,
        *,
        repeat_threshold: int = 3,
        max_cycle_length: int = 4,
        window: int = 24,
    ) -> LoopDetection:
        if repeat_threshold < 2 or max_cycle_length < 1 or window < 2:
            raise ValueError("invalid loop detection limits")
        digests = [state.digest for state in tuple(self._states)[-window:]]
        if len(digests) < repeat_threshold:
            return LoopDetection(False)
        if len(set(digests[-repeat_threshold:])) == 1:
            return LoopDetection(
                True,
                "repeated_state",
                1,
                repeat_threshold,
                tuple(digests[-repeat_threshold:]),
            )
        maximum = min(max_cycle_length, len(digests) // repeat_threshold)
        for cycle_length in range(2, maximum + 1):
            sample_length = cycle_length * repeat_threshold
            sample = digests[-sample_length:]
            cycle = sample[-cycle_length:]
            if cycle * repeat_threshold == sample:
                return LoopDetection(
                    True,
                    "state_cycle",
                    cycle_length,
                    repeat_threshold,
                    tuple(sample),
                )
        return LoopDetection(False)
