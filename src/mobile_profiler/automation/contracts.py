"""Stable data contracts for the task-aware mobile automation kernel.

This module deliberately contains no ADB, uiautomator2, model-provider, or UI
code.  Adapters may depend on these contracts; the contracts must not depend on
any adapter.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _name(value: object, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _NAME_RE.fullmatch(normalized):
        raise ValueError(
            f"{field_name} must start with an alphanumeric character and contain "
            "only letters, numbers, '.', '_', ':', or '-'"
        )
    return normalized


def _names(values: Sequence[str] | frozenset[str], field_name: str) -> frozenset[str]:
    return frozenset(_name(value, field_name) for value in values)


def _mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): item for key, item in value.items()}


class ActionRisk(str, Enum):
    """Risk assigned by the host, never by the model alone."""

    LOW = "low"
    SENSITIVE = "sensitive"
    DESTRUCTIVE = "destructive"


class ActionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    STALE = "stale"
    CANCELLED = "cancelled"


class VerificationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    ERROR = "error"


class VerificationMode(str, Enum):
    ALL = "all"
    ANY = "any"


class ApprovalStatus(str, Enum):
    APPROVED = "approved"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


class InterventionKind(str, Enum):
    OBSERVE_ONLY = "observe_only"
    SUGGEST_ACTION = "suggest_action"
    REQUIRE_APPROVAL = "require_approval"
    ABORT = "abort"


class AgentDirectiveKind(str, Enum):
    ACTION = "action"
    SKILL = "skill"
    FINISH = "finish"
    TAKE_OVER = "take_over"


@dataclass(frozen=True)
class Bounds:
    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        if self.right < self.left or self.bottom < self.top:
            raise ValueError("bounds must satisfy right >= left and bottom >= top")

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)

    def normalized(self, width: int, height: int, scale: int = 1000) -> tuple[int, ...]:
        if width <= 0 or height <= 0 or scale <= 0:
            raise ValueError("width, height, and scale must be positive")
        return (
            round(self.left / width * scale),
            round(self.top / height * scale),
            round(self.right / width * scale),
            round(self.bottom / height * scale),
        )


@dataclass(frozen=True)
class Artifact:
    """In-memory or externally persisted evidence with an optional digest."""

    artifact_id: str
    media_type: str
    data: bytes = b""
    uri: str = ""
    sha256: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _name(self.artifact_id, "artifact_id"))
        media_type = str(self.media_type or "").strip().lower()
        if "/" not in media_type:
            raise ValueError("media_type must be a MIME type")
        if not self.data and not str(self.uri or "").strip():
            raise ValueError("artifact requires data or uri")
        digest = str(self.sha256 or "").strip().lower()
        if self.data:
            calculated = hashlib.sha256(self.data).hexdigest()
            if digest and digest != calculated:
                raise ValueError("artifact sha256 does not match data")
            digest = calculated
        elif digest and not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("artifact sha256 must be a lowercase hexadecimal digest")
        object.__setattr__(self, "media_type", media_type)
        object.__setattr__(self, "uri", str(self.uri or "").strip())
        object.__setattr__(self, "sha256", digest)
        object.__setattr__(self, "metadata", _mapping(self.metadata))


@dataclass(frozen=True)
class ScreenFrame:
    artifact: Artifact
    width: int
    height: int
    rotation: int = 0

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("screen dimensions must be positive")
        if self.rotation not in {0, 90, 180, 270}:
            raise ValueError("rotation must be 0, 90, 180, or 270")
        if not self.artifact.media_type.startswith("image/"):
            raise ValueError("screen artifact must be an image")


@dataclass(frozen=True)
class UiElement:
    """A compact semantic element; IDs are valid only for one revision."""

    element_id: str
    bounds: Bounds
    text: str = ""
    resource_id: str = ""
    content_description: str = ""
    class_name: str = ""
    package: str = ""
    parent_id: Optional[str] = None
    clickable: bool = False
    enabled: bool = True
    focusable: bool = False
    scrollable: bool = False
    selected: bool = False
    checked: Optional[bool] = None
    visible: bool = True
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "element_id", _name(self.element_id, "element_id"))
        if self.parent_id is not None:
            object.__setattr__(self, "parent_id", _name(self.parent_id, "parent_id"))
        for field_name in (
            "text",
            "resource_id",
            "content_description",
            "class_name",
            "package",
        ):
            object.__setattr__(self, field_name, str(getattr(self, field_name) or ""))
        object.__setattr__(self, "attributes", _mapping(self.attributes))


@dataclass(frozen=True)
class UiHierarchy:
    revision: str
    width: int
    height: int
    elements: tuple[UiElement, ...]
    source: str
    raw_artifact: Optional[Artifact] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "revision", _name(self.revision, "revision"))
        object.__setattr__(self, "source", _name(self.source, "source"))
        object.__setattr__(self, "elements", tuple(self.elements))
        if self.width <= 0 or self.height <= 0:
            raise ValueError("hierarchy dimensions must be positive")
        ids = [element.element_id for element in self.elements]
        if len(ids) != len(set(ids)):
            raise ValueError("element_id values must be unique within one hierarchy")

    def element(self, element_id: str) -> UiElement:
        normalized = _name(element_id, "element_id")
        for element in self.elements:
            if element.element_id == normalized:
                return element
        raise KeyError(normalized)


@dataclass(frozen=True)
class DeviceContext:
    device_id: str
    foreground_package: str = ""
    foreground_activity: str = ""
    orientation: str = "unknown"
    screen_on: Optional[bool] = None
    locked: Optional[bool] = None
    ime_visible: Optional[bool] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "device_id", _name(self.device_id, "device_id"))
        object.__setattr__(self, "metadata", _mapping(self.metadata))


@dataclass(frozen=True)
class ObservationRequest:
    channels: frozenset[str] = field(default_factory=lambda: frozenset({"screenshot"}))
    previous_revision: str = ""
    timeout_s: float = 20.0
    max_ui_elements: int = 500
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "channels", _names(self.channels, "observation channel"))
        if not self.channels:
            raise ValueError("at least one observation channel is required")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if self.max_ui_elements <= 0:
            raise ValueError("max_ui_elements must be positive")
        object.__setattr__(self, "previous_revision", str(self.previous_revision or "").strip())
        object.__setattr__(self, "metadata", _mapping(self.metadata))


@dataclass(frozen=True)
class Observation:
    revision: str
    captured_at: float
    channels: frozenset[str]
    context: DeviceContext
    screen: Optional[ScreenFrame] = None
    ui: Optional[UiHierarchy] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "revision", _name(self.revision, "revision"))
        object.__setattr__(self, "channels", _names(self.channels, "observation channel"))
        if self.captured_at <= 0:
            raise ValueError("captured_at must be a positive epoch timestamp")
        if self.ui is not None and self.ui.revision != self.revision:
            raise ValueError("UI hierarchy revision must match observation revision")
        object.__setattr__(self, "metadata", _mapping(self.metadata))


@dataclass(frozen=True)
class DeviceCapabilities:
    device_id: str
    observation_channels: frozenset[str]
    action_names: frozenset[str]
    features: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "device_id", _name(self.device_id, "device_id"))
        object.__setattr__(
            self,
            "observation_channels",
            _names(self.observation_channels, "observation channel"),
        )
        object.__setattr__(self, "action_names", _names(self.action_names, "action name"))
        object.__setattr__(self, "features", _mapping(self.features))


@dataclass(frozen=True)
class DeviceHealth:
    ready: bool
    message: str = ""
    recoverable: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", str(self.message or ""))
        object.__setattr__(self, "metadata", _mapping(self.metadata))


@dataclass(frozen=True)
class TaskCapabilityProfile:
    """Best-fit observation/action surface selected for one test item."""

    profile_id: str
    observations: frozenset[str]
    actions: frozenset[str]
    verifiers: frozenset[str] = field(default_factory=frozenset)
    watchers: frozenset[str] = field(default_factory=frozenset)
    skills: frozenset[str] = field(default_factory=frozenset)
    settle_strategy: str = "ui_tree_stable"
    allowed_risks: frozenset[ActionRisk] = field(
        default_factory=lambda: frozenset({ActionRisk.LOW})
    )
    max_steps: int = 100
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _name(self.profile_id, "profile_id"))
        for field_name in ("observations", "actions", "verifiers", "watchers", "skills"):
            object.__setattr__(
                self,
                field_name,
                _names(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "settle_strategy",
            _name(self.settle_strategy, "settle_strategy"),
        )
        object.__setattr__(
            self,
            "allowed_risks",
            frozenset(ActionRisk(value) for value in self.allowed_risks),
        )
        if not self.observations or not self.actions:
            raise ValueError("profiles require at least one observation and action")
        if self.max_steps <= 0:
            raise ValueError("max_steps must be positive")
        object.__setattr__(self, "metadata", _mapping(self.metadata))

    def allows(self, action: "ActionRequest") -> bool:
        return action.name in self.actions and action.risk in self.allowed_risks


@dataclass(frozen=True)
class ActionRequest:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    observation_revision: str = ""
    risk: ActionRisk = ActionRisk.LOW
    request_id: str = field(default_factory=lambda: f"action-{uuid.uuid4().hex}")
    source: str = "agent"

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name, "action name"))
        object.__setattr__(self, "request_id", _name(self.request_id, "request_id"))
        object.__setattr__(self, "source", _name(self.source, "source"))
        arguments = _mapping(self.arguments)
        object.__setattr__(self, "risk", ActionRisk(self.risk))
        revision = str(self.observation_revision or "").strip()
        if "element_id" in arguments:
            arguments["element_id"] = _name(arguments["element_id"], "element_id")
            if not revision:
                raise ValueError("element actions require observation_revision")
        object.__setattr__(self, "arguments", arguments)
        object.__setattr__(self, "observation_revision", revision)

    @classmethod
    def tap_element(
        cls,
        element_id: str,
        observation_revision: str,
        *,
        source: str = "agent",
    ) -> "ActionRequest":
        revision = _name(observation_revision, "observation_revision")
        return cls(
            name="tap_element",
            arguments={"element_id": _name(element_id, "element_id")},
            observation_revision=revision,
            source=source,
        )


@dataclass(frozen=True)
class ActionResult:
    request_id: str
    name: str
    status: ActionStatus
    message: str = ""
    before_revision: str = ""
    after_revision: str = ""
    evidence_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _name(self.request_id, "request_id"))
        object.__setattr__(self, "name", _name(self.name, "action name"))
        object.__setattr__(self, "status", ActionStatus(self.status))
        object.__setattr__(self, "message", str(self.message or ""))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "metadata", _mapping(self.metadata))


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""
    requires_approval: bool = False
    normalized_action: Optional[ActionRequest] = None


@dataclass(frozen=True)
class ApprovalDecision:
    status: ApprovalStatus
    reason: str = ""
    decided_by: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ApprovalStatus(self.status))


@dataclass(frozen=True)
class VerificationRequest:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: f"verify-{uuid.uuid4().hex}")

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name, "verifier name"))
        object.__setattr__(self, "request_id", _name(self.request_id, "request_id"))
        object.__setattr__(self, "arguments", _mapping(self.arguments))


@dataclass(frozen=True)
class VerificationResult:
    request_id: str
    name: str
    status: VerificationStatus
    message: str = ""
    evidence_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _name(self.request_id, "request_id"))
        object.__setattr__(self, "name", _name(self.name, "verifier name"))
        object.__setattr__(self, "status", VerificationStatus(self.status))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "metadata", _mapping(self.metadata))


@dataclass(frozen=True)
class VerificationPlan:
    requests: tuple[VerificationRequest, ...]
    mode: VerificationMode = VerificationMode.ALL
    plan_id: str = field(default_factory=lambda: f"verify-plan-{uuid.uuid4().hex}")

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _name(self.plan_id, "plan_id"))
        object.__setattr__(self, "mode", VerificationMode(self.mode))
        object.__setattr__(self, "requests", tuple(self.requests))
        if not self.requests:
            raise ValueError("verification plan requires at least one request")
        request_ids = [request.request_id for request in self.requests]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("verification request IDs must be unique within a plan")


@dataclass(frozen=True)
class VerificationReport:
    plan_id: str
    status: VerificationStatus
    results: tuple[VerificationResult, ...]
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _name(self.plan_id, "plan_id"))
        object.__setattr__(self, "status", VerificationStatus(self.status))
        object.__setattr__(self, "results", tuple(self.results))
        object.__setattr__(self, "message", str(self.message or ""))


@dataclass(frozen=True)
class ComponentDescriptor:
    name: str
    version: str
    description: str = ""
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name, "component name"))
        object.__setattr__(self, "version", _name(self.version, "component version"))
        object.__setattr__(
            self,
            "capabilities",
            _names(self.capabilities, "component capability"),
        )


@dataclass(frozen=True)
class WatcherIntervention:
    watcher: str
    kind: InterventionKind
    reason: str
    priority: int = 0
    proposed_actions: tuple[ActionRequest, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "watcher", _name(self.watcher, "watcher name"))
        object.__setattr__(self, "kind", InterventionKind(self.kind))
        object.__setattr__(self, "proposed_actions", tuple(self.proposed_actions))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))


@dataclass(frozen=True)
class SkillRequest:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: f"skill-{uuid.uuid4().hex}")
    timeout_s: float = 120.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name, "skill name"))
        object.__setattr__(self, "request_id", _name(self.request_id, "request_id"))
        object.__setattr__(self, "arguments", _mapping(self.arguments))
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")


@dataclass(frozen=True)
class SkillResult:
    request_id: str
    name: str
    succeeded: bool
    message: str = ""
    action_results: tuple[ActionResult, ...] = ()
    verification_results: tuple[VerificationResult, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _name(self.request_id, "request_id"))
        object.__setattr__(self, "name", _name(self.name, "skill name"))
        object.__setattr__(self, "action_results", tuple(self.action_results))
        object.__setattr__(self, "verification_results", tuple(self.verification_results))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "metadata", _mapping(self.metadata))


@dataclass(frozen=True)
class AgentRequest:
    """Provider-neutral input for one Qwen or other planner decision."""

    objective: str
    observation: Observation
    profile: TaskCapabilityProfile
    recent_actions: tuple[ActionResult, ...] = ()
    step_index: int = 0
    max_steps: int = 100
    request_id: str = field(default_factory=lambda: f"agent-{uuid.uuid4().hex}")
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        objective = str(self.objective or "").strip()
        if not objective:
            raise ValueError("agent objective cannot be empty")
        object.__setattr__(self, "objective", objective)
        object.__setattr__(self, "request_id", _name(self.request_id, "request_id"))
        object.__setattr__(self, "recent_actions", tuple(self.recent_actions))
        object.__setattr__(self, "metadata", _mapping(self.metadata))
        if self.step_index < 0 or self.max_steps <= 0 or self.step_index >= self.max_steps:
            raise ValueError("agent step_index must be within max_steps")


@dataclass(frozen=True)
class AgentDecision:
    """Exactly one typed directive; finish remains subject to host verification."""

    request_id: str
    kind: AgentDirectiveKind
    action: Optional[ActionRequest] = None
    skill: Optional[SkillRequest] = None
    reasoning: str = ""
    message: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _name(self.request_id, "request_id"))
        object.__setattr__(self, "kind", AgentDirectiveKind(self.kind))
        object.__setattr__(self, "reasoning", str(self.reasoning or ""))
        object.__setattr__(self, "message", str(self.message or ""))
        object.__setattr__(self, "metadata", _mapping(self.metadata))
        if self.kind == AgentDirectiveKind.ACTION:
            if self.action is None or self.skill is not None:
                raise ValueError("action directive requires only action")
        elif self.kind == AgentDirectiveKind.SKILL:
            if self.skill is None or self.action is not None:
                raise ValueError("skill directive requires only skill")
        elif self.action is not None or self.skill is not None:
            raise ValueError("terminal agent directives cannot carry action or skill")


@dataclass(frozen=True)
class AutomationEvent:
    event_type: str
    payload: Mapping[str, Any]
    timestamp: float = field(default_factory=time.time)
    run_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", _name(self.event_type, "event_type"))
        object.__setattr__(self, "payload", _mapping(self.payload))
        if self.timestamp <= 0:
            raise ValueError("timestamp must be positive")


@dataclass(frozen=True)
class SettleResult:
    observation: Observation
    stable: bool
    elapsed_s: float
    sample_count: int
    reason: str = ""

    def __post_init__(self) -> None:
        if self.elapsed_s < 0 or self.sample_count <= 0:
            raise ValueError("settle result requires non-negative elapsed time and samples")
