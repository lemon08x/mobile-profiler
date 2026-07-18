"""Provider-neutral scenario graph contracts and static validation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional

from .contracts import TaskCapabilityProfile, _mapping, _name, _names


SCENARIO_COMPLETE = "$complete"
SCENARIO_FAILED = "$failed"
SCENARIO_TAKE_OVER = "$take_over"
SCENARIO_TERMINALS = frozenset(
    {SCENARIO_COMPLETE, SCENARIO_FAILED, SCENARIO_TAKE_OVER}
)


class OperationKind(str, Enum):
    ACTION = "action"
    SKILL = "skill"
    AGENT = "agent"


class ValidationSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class ScenarioRunStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    TAKE_OVER = "take_over"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class OperationCall:
    kind: OperationKind
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", OperationKind(self.kind))
        object.__setattr__(self, "name", _name(self.name, "operation name"))
        object.__setattr__(self, "arguments", _mapping(self.arguments))


@dataclass(frozen=True)
class ScenarioNode:
    node_id: str
    operation: OperationCall
    on_success: str = SCENARIO_COMPLETE
    on_failure: str = SCENARIO_FAILED
    verifiers: tuple[str, ...] = ()
    watchers: tuple[str, ...] = ()
    max_attempts: int = 1
    timeout_s: float = 120.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _name(self.node_id, "node_id"))
        for field_name in ("on_success", "on_failure"):
            target = str(getattr(self, field_name) or "").strip()
            if target not in SCENARIO_TERMINALS:
                target = _name(target, field_name)
            object.__setattr__(self, field_name, target)
        object.__setattr__(
            self,
            "verifiers",
            tuple(dict.fromkeys(_name(value, "verifier name") for value in self.verifiers)),
        )
        object.__setattr__(
            self,
            "watchers",
            tuple(dict.fromkeys(_name(value, "watcher name") for value in self.watchers)),
        )
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")


@dataclass(frozen=True)
class ScenarioDefinition:
    scenario_id: str
    entry_node: str
    nodes: tuple[ScenarioNode, ...]
    setup: tuple[OperationCall, ...] = ()
    cleanup: tuple[OperationCall, ...] = ()
    final_verifiers: tuple[str, ...] = ()
    max_transitions: int = 200
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario_id", _name(self.scenario_id, "scenario_id"))
        object.__setattr__(self, "entry_node", _name(self.entry_node, "entry_node"))
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "setup", tuple(self.setup))
        object.__setattr__(self, "cleanup", tuple(self.cleanup))
        object.__setattr__(
            self,
            "final_verifiers",
            tuple(
                dict.fromkeys(
                    _name(value, "verifier name") for value in self.final_verifiers
                )
            ),
        )
        object.__setattr__(self, "metadata", _mapping(self.metadata))
        if self.max_transitions <= 0:
            raise ValueError("max_transitions must be positive")


@dataclass(frozen=True)
class ComponentCatalog:
    actions: frozenset[str] = field(default_factory=frozenset)
    skills: frozenset[str] = field(default_factory=frozenset)
    agents: frozenset[str] = field(default_factory=frozenset)
    verifiers: frozenset[str] = field(default_factory=frozenset)
    watchers: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        for field_name in ("actions", "skills", "agents", "verifiers", "watchers"):
            object.__setattr__(self, field_name, _names(getattr(self, field_name), field_name))

    def operations(self, kind: OperationKind) -> frozenset[str]:
        return {
            OperationKind.ACTION: self.actions,
            OperationKind.SKILL: self.skills,
            OperationKind.AGENT: self.agents,
        }[kind]


@dataclass(frozen=True)
class ScenarioValidationIssue:
    severity: ValidationSeverity
    code: str
    message: str
    node_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "severity", ValidationSeverity(self.severity))
        object.__setattr__(self, "code", _name(self.code, "validation code"))


@dataclass(frozen=True)
class ScenarioValidationResult:
    issues: tuple[ScenarioValidationIssue, ...]

    @property
    def valid(self) -> bool:
        return not any(issue.severity == ValidationSeverity.ERROR for issue in self.issues)

    @property
    def errors(self) -> tuple[ScenarioValidationIssue, ...]:
        return tuple(
            issue for issue in self.issues if issue.severity == ValidationSeverity.ERROR
        )

    @property
    def warnings(self) -> tuple[ScenarioValidationIssue, ...]:
        return tuple(
            issue for issue in self.issues if issue.severity == ValidationSeverity.WARNING
        )


@dataclass(frozen=True)
class ScenarioRunRequest:
    definition: ScenarioDefinition
    profile: TaskCapabilityProfile
    inputs: Mapping[str, Any] = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: f"scenario-{uuid.uuid4().hex}")

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _name(self.run_id, "run_id"))
        object.__setattr__(self, "inputs", _mapping(self.inputs))


@dataclass(frozen=True)
class ScenarioStepResult:
    node_id: str
    attempt: int
    succeeded: bool
    message: str = ""


@dataclass(frozen=True)
class ScenarioRunResult:
    run_id: str
    status: ScenarioRunStatus
    steps: tuple[ScenarioStepResult, ...]
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ScenarioRunStatus(self.status))
        object.__setattr__(self, "steps", tuple(self.steps))


class ScenarioGraphValidator:
    """Checks a pipeline without loading any device, model, or third-party adapter."""

    def validate(
        self,
        definition: ScenarioDefinition,
        catalog: ComponentCatalog,
        profile: Optional[TaskCapabilityProfile] = None,
    ) -> ScenarioValidationResult:
        issues: list[ScenarioValidationIssue] = []
        node_by_id: dict[str, ScenarioNode] = {}
        duplicates: set[str] = set()
        for node in definition.nodes:
            if node.node_id in node_by_id:
                duplicates.add(node.node_id)
            else:
                node_by_id[node.node_id] = node
        for node_id in sorted(duplicates):
            issues.append(
                self._error("duplicate_node", f"duplicate node id: {node_id}", node_id)
            )
        if not definition.nodes:
            issues.append(self._error("empty_scenario", "scenario has no nodes"))
        if definition.entry_node not in node_by_id:
            issues.append(
                self._error(
                    "missing_entry",
                    f"entry node is not defined: {definition.entry_node}",
                    definition.entry_node,
                )
            )

        for operation in definition.setup:
            self._validate_operation(operation, "setup", catalog, profile, issues)
        for operation in definition.cleanup:
            self._validate_operation(operation, "cleanup", catalog, profile, issues)

        for verifier in definition.final_verifiers:
            if verifier not in catalog.verifiers:
                issues.append(
                    self._error(
                        "unknown_verifier",
                        f"final verifier is not registered: {verifier}",
                    )
                )
            elif profile is not None and verifier not in profile.verifiers:
                issues.append(
                    self._error(
                        "profile_disallows_verifier",
                        f"final verifier is not enabled by profile: {verifier}",
                    )
                )

        has_terminal_edge = False
        for node in node_by_id.values():
            self._validate_operation(
                node.operation,
                node.node_id,
                catalog,
                profile,
                issues,
            )
            for target_name, target in (
                ("on_success", node.on_success),
                ("on_failure", node.on_failure),
            ):
                if target in SCENARIO_TERMINALS:
                    has_terminal_edge = True
                elif target not in node_by_id:
                    issues.append(
                        self._error(
                            "missing_transition",
                            f"{target_name} target is not defined: {target}",
                            node.node_id,
                        )
                    )
            for verifier in node.verifiers:
                if verifier not in catalog.verifiers:
                    issues.append(
                        self._error(
                            "unknown_verifier",
                            f"verifier is not registered: {verifier}",
                            node.node_id,
                        )
                    )
                elif profile is not None and verifier not in profile.verifiers:
                    issues.append(
                        self._error(
                            "profile_disallows_verifier",
                            f"verifier is not enabled by profile: {verifier}",
                            node.node_id,
                        )
                    )
            for watcher in node.watchers:
                if watcher not in catalog.watchers:
                    issues.append(
                        self._error(
                            "unknown_watcher",
                            f"watcher is not registered: {watcher}",
                            node.node_id,
                        )
                    )
                elif profile is not None and watcher not in profile.watchers:
                    issues.append(
                        self._error(
                            "profile_disallows_watcher",
                            f"watcher is not enabled by profile: {watcher}",
                            node.node_id,
                        )
                    )
        if definition.nodes and not has_terminal_edge:
            issues.append(
                self._error(
                    "no_terminal",
                    "scenario graph has no terminal transition",
                )
            )

        if definition.entry_node in node_by_id:
            reachable = self._reachable(definition.entry_node, node_by_id)
            for node_id in sorted(set(node_by_id) - reachable):
                issues.append(
                    ScenarioValidationIssue(
                        ValidationSeverity.WARNING,
                        "unreachable_node",
                        f"node cannot be reached from entry: {node_id}",
                        node_id,
                    )
                )
        return ScenarioValidationResult(tuple(issues))

    @staticmethod
    def _validate_operation(
        operation: OperationCall,
        owner: str,
        catalog: ComponentCatalog,
        profile: Optional[TaskCapabilityProfile],
        issues: list[ScenarioValidationIssue],
    ) -> None:
        if operation.name not in catalog.operations(operation.kind):
            issues.append(
                ScenarioGraphValidator._error(
                    "unknown_operation",
                    f"{operation.kind.value} is not registered: {operation.name}",
                    owner if owner not in {"setup", "cleanup"} else "",
                )
            )
            return
        profile_components = None
        if profile is not None:
            if operation.kind == OperationKind.ACTION:
                profile_components = profile.actions
            elif operation.kind == OperationKind.SKILL:
                profile_components = profile.skills
        if profile_components is not None and operation.name not in profile_components:
            issues.append(
                ScenarioGraphValidator._error(
                    f"profile_disallows_{operation.kind.value}",
                    f"{operation.kind.value} is not enabled by profile: {operation.name}",
                    owner if owner not in {"setup", "cleanup"} else "",
                )
            )

    @staticmethod
    def _reachable(entry: str, nodes: Mapping[str, ScenarioNode]) -> set[str]:
        pending = [entry]
        visited: set[str] = set()
        while pending:
            node_id = pending.pop()
            if node_id in visited or node_id not in nodes:
                continue
            visited.add(node_id)
            node = nodes[node_id]
            for target in (node.on_success, node.on_failure):
                if target not in SCENARIO_TERMINALS:
                    pending.append(target)
        return visited

    @staticmethod
    def _error(code: str, message: str, node_id: str = "") -> ScenarioValidationIssue:
        return ScenarioValidationIssue(ValidationSeverity.ERROR, code, message, node_id)
