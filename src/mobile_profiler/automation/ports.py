"""Dependency-inversion ports for mobile automation adapters and engines."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

from .contracts import (
    AgentDecision,
    AgentRequest,
    ActionRequest,
    ActionResult,
    ApprovalDecision,
    Artifact,
    AutomationEvent,
    ComponentDescriptor,
    DeviceCapabilities,
    DeviceHealth,
    Observation,
    ObservationRequest,
    PolicyDecision,
    SettleResult,
    SkillRequest,
    SkillResult,
    TaskCapabilityProfile,
    VerificationRequest,
    VerificationPlan,
    VerificationReport,
    VerificationResult,
    WatcherIntervention,
)

if TYPE_CHECKING:
    from .scenario import (
        ComponentCatalog,
        ScenarioDefinition,
        ScenarioRunRequest,
        ScenarioRunResult,
        ScenarioValidationResult,
    )


@runtime_checkable
class DeviceGateway(Protocol):
    """Typed device actions and liveness; never an arbitrary shell escape hatch."""

    def capabilities(self) -> DeviceCapabilities:
        ...

    def health(self) -> DeviceHealth:
        ...

    def perform(self, action: ActionRequest) -> ActionResult:
        ...

    def recover(self) -> DeviceHealth:
        ...


@runtime_checkable
class SemanticUiProvider(Protocol):
    """Produces task-selected visual and semantic observations."""

    def observe(self, request: ObservationRequest) -> Observation:
        ...

    def recover(self) -> bool:
        ...


@runtime_checkable
class ActionPolicy(Protocol):
    def evaluate(
        self,
        action: ActionRequest,
        profile: TaskCapabilityProfile,
        observation: Optional[Observation],
    ) -> PolicyDecision:
        ...


@runtime_checkable
class ApprovalGate(Protocol):
    def decide(self, action: ActionRequest, policy: PolicyDecision) -> ApprovalDecision:
        ...


@runtime_checkable
class UiSettler(Protocol):
    def wait_until_stable(
        self,
        previous: Observation,
        request: ObservationRequest,
    ) -> SettleResult:
        ...


@runtime_checkable
class Verifier(Protocol):
    @property
    def descriptor(self) -> ComponentDescriptor:
        ...

    def verify(
        self,
        request: VerificationRequest,
        observation: Observation,
    ) -> VerificationResult:
        ...


@runtime_checkable
class VerifierEngine(Protocol):
    """Combines deterministic verifiers without treating model finish as success."""

    def evaluate(
        self,
        plan: VerificationPlan,
        observation: Observation,
    ) -> VerificationReport:
        ...


@runtime_checkable
class Watcher(Protocol):
    @property
    def descriptor(self) -> ComponentDescriptor:
        ...

    def inspect(
        self,
        observation: Observation,
        profile: TaskCapabilityProfile,
    ) -> Optional[WatcherIntervention]:
        ...


@runtime_checkable
class EvidenceSink(Protocol):
    def record(self, event: AutomationEvent) -> None:
        ...

    def store(self, artifact: Artifact) -> str:
        ...


@runtime_checkable
class SkillRuntime(Protocol):
    """Narrow host facade available to deterministic or real-time skills."""

    @property
    def profile(self) -> TaskCapabilityProfile:
        ...

    def observe(self, request: ObservationRequest) -> Observation:
        ...

    def act(self, action: ActionRequest) -> ActionResult:
        ...

    def verify(self, request: VerificationRequest) -> VerificationResult:
        ...

    def emit(self, event: AutomationEvent) -> None:
        ...

    def stop_requested(self) -> bool:
        ...


@runtime_checkable
class Skill(Protocol):
    @property
    def descriptor(self) -> ComponentDescriptor:
        ...

    def execute(self, request: SkillRequest, runtime: SkillRuntime) -> SkillResult:
        ...


@runtime_checkable
class AgentPlanner(Protocol):
    """One provider-neutral planning step; model transport stays behind this port."""

    @property
    def descriptor(self) -> ComponentDescriptor:
        ...

    def decide(self, request: AgentRequest) -> AgentDecision:
        ...


@runtime_checkable
class ScenarioEngine(Protocol):
    """Execution contract only; no engine is wired into the product yet."""

    def validate(
        self,
        definition: "ScenarioDefinition",
        catalog: "ComponentCatalog",
        profile: Optional[TaskCapabilityProfile] = None,
    ) -> "ScenarioValidationResult":
        ...

    def run(
        self,
        request: "ScenarioRunRequest",
        runtime: SkillRuntime,
    ) -> "ScenarioRunResult":
        ...
