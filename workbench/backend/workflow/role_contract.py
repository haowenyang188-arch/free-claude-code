"""Role Contract -- executable definition of agent responsibility boundaries.

This module is the **system contract** for who may do what in the SOP
Workbench.  It replaces every oral agreement about agent roles with an
importable, assertable, test-verifiable definition.

Frozen role model (Session 3 / Role Contract Verification -- do NOT redesign):

    SOP_ENGINE  Sole owner of Task / StepRun / SopRun status and flow routing.
    CLAUDE      Planner.  Understands requirements, reads code, produces PLAN,
                makes development decisions, supplies the fix plan on rework.
                NOT the final reviewer.  MUST NOT declare a task complete.
    DSH         Executor.  Applies the already-decided plan: code edits,
                commands, tests.  MUST NOT redefine requirements, MUST NOT own
                final review, MUST NOT change the SOP flow.
    CODEX       Final independent reviewer.  Inspects diff / tests / bugs /
                edge cases / regressions / requirement coverage.  PASS|REJECT.
                MUST NOT bypass the SOP Engine to change final flow state.

Hard invariants enforced here:

    I1  Only SOP_ENGINE may write any SOP status field.
    I2  No agent may declare completion; terminality belongs to the Engine.
    I3  No agent may redefine requirements (PLAN ownership is Claude's, and
        only via a routed PLAN_INVALID return, never in-place).
    I4  No agent-to-agent P2P dispatch; the only legal channel is the
        Engine-owned Handoff.
    I5  Artifact is the sole fact carrier between stages -- never chat history.
    I6  Rework routing is decided by the Engine through an explicit table;
        no agent or script may decide where a rejected task goes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..domain.models import (
    ArtifactType,
    SopRunStatus,
    StepStatus,
    TaskStatus,
)

__all__ = [
    "AGENT_ROLES",
    "CONTRACT_DEBT_REGISTER",
    "KNOWN_VIOLATIONS",
    "REVIEW_ROUTING",
    "ROLE_CAPABILITIES",
    "SOURCE_RULES",
    "STAGE_OUTPUT_ARTIFACT_TYPES",
    "STAGE_OWNER",
    "STATUS_AUTHORITY",
    "TERMINAL_RUN_STATUSES",
    "TERMINAL_STEP_STATUSES",
    "TERMINAL_TASK_STATUSES",
    "AgentRole",
    "Capability",
    "ContractViolation",
    "EngineCondition",
    "ReviewVerdict",
    "RoleContractViolation",
    "RouteTarget",
    "SourceRule",
    "Stage",
    "apply_status",
    "assert_capability",
    "can",
    "non_terminal_task_statuses",
    "require_engine",
    "resolve_route",
    "validate_stage_output",
]


class AgentRole(str, Enum):
    """The pipeline roles.

    2026-09-06 用户定版：聊天机器人 = Claude/Codex 双机器人；SOP 流水线改为
    Claude 方案/修订 + Codex 审核/复审/最终门 + Codex 执行者落地。为此在原
    四角色基础上新增 CODEX_EXECUTOR（可写沙箱的执行者，与只读审核者 CODEX
    严格区分——F-5 的评审身份来源仍是 SubagentAssignment.role_id）。
    DSH 保留为遗留角色：仅用于历史 run 的数据兼容，新流水线不再分配。
    """

    SOP_ENGINE = "sop_engine"
    CLAUDE = "claude"
    DSH = "dsh"  # legacy：历史 run 兼容；新流水线不再分配
    CODEX = "codex"
    CODEX_EXECUTOR = "codex_executor"


AGENT_ROLES: frozenset[AgentRole] = frozenset(
    {AgentRole.CLAUDE, AgentRole.DSH, AgentRole.CODEX, AgentRole.CODEX_EXECUTOR}
)


class Capability(str, Enum):
    """Everything a role may or may not do.  Denied capabilities are listed
    explicitly so that audits can reference them by name."""

    # ---- production capabilities -------------------------------------
    PRODUCE_PLAN = "produce_plan"
    INSPECT_WORKSPACE = "inspect_workspace"
    MODIFY_CODE = "modify_code"
    RUN_COMMANDS = "run_commands"
    RUN_TESTS = "run_tests"
    PRODUCE_ARTIFACT = "produce_artifact"
    EMIT_REVIEW_VERDICT = "emit_review_verdict"

    # ---- transition capabilities (SOP_ENGINE exclusive) --------------
    TRANSITION_TASK = "transition_task"
    TRANSITION_STEP = "transition_step"
    TRANSITION_ATTEMPT = "transition_attempt"
    TRANSITION_RUN = "transition_run"
    TRANSITION_HANDOFF = "transition_handoff"
    TRANSITION_REVIEW = "transition_review"
    ROUTE_REWORK = "route_rework"

    # ---- forbidden capabilities (never granted to anyone) ------------
    DECLARE_COMPLETION = "declare_completion"
    REDEFINE_REQUIREMENT = "redefine_requirement"
    CHANGE_FLOW = "change_flow"
    P2P_DISPATCH = "p2p_dispatch"


TRANSITION_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.TRANSITION_TASK,
        Capability.TRANSITION_STEP,
        Capability.TRANSITION_ATTEMPT,
        Capability.TRANSITION_RUN,
        Capability.TRANSITION_HANDOFF,
        Capability.TRANSITION_REVIEW,
        Capability.ROUTE_REWORK,
    }
)

#: Capabilities that exist only so they can be denied.  No role may hold them.
FORBIDDEN_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.DECLARE_COMPLETION,
        Capability.REDEFINE_REQUIREMENT,
        Capability.CHANGE_FLOW,
        Capability.P2P_DISPATCH,
    }
)

ROLE_CAPABILITIES: dict[AgentRole, frozenset[Capability]] = {
    AgentRole.SOP_ENGINE: frozenset(TRANSITION_CAPABILITIES),
    AgentRole.CLAUDE: frozenset(
        {
            Capability.PRODUCE_PLAN,
            Capability.INSPECT_WORKSPACE,
            Capability.PRODUCE_ARTIFACT,
        }
    ),
    AgentRole.DSH: frozenset(
        {
            Capability.MODIFY_CODE,
            Capability.RUN_COMMANDS,
            Capability.RUN_TESTS,
            Capability.INSPECT_WORKSPACE,
            Capability.PRODUCE_ARTIFACT,
        }
    ),
    # 遗留：历史 run 兼容，新流水线不再分配（见 AgentRole docstring）。
    AgentRole.CODEX: frozenset(
        {
            Capability.INSPECT_WORKSPACE,
            Capability.PRODUCE_ARTIFACT,
            Capability.EMIT_REVIEW_VERDICT,
        }
    ),
    # Codex 执行者：与只读审核者 CODEX 严格区分（用户定版 2026-09-06），
    # 继承原执行角色（DSH）的生产能力，但无评审裁决权。
    AgentRole.CODEX_EXECUTOR: frozenset(
        {
            Capability.MODIFY_CODE,
            Capability.RUN_COMMANDS,
            Capability.RUN_TESTS,
            Capability.INSPECT_WORKSPACE,
            Capability.PRODUCE_ARTIFACT,
        }
    ),
}


class RoleContractViolation(RuntimeError):
    """Raised when a role attempts a capability it does not hold."""


#: Backwards/forwards compatible alias.
ContractViolation = RoleContractViolation


def can(role: AgentRole, capability: Capability) -> bool:
    """Return True when ``role`` holds ``capability``."""
    return capability in ROLE_CAPABILITIES.get(role, frozenset())


def assert_capability(role: AgentRole, capability: Capability, *, context: str = "") -> None:
    """Raise :class:`RoleContractViolation` when ``role`` lacks ``capability``.

    ``context`` is free-form but should name the operation being attempted so
    that the failure is actionable.
    """
    if capability in FORBIDDEN_CAPABILITIES:
        raise RoleContractViolation(
            f"capability {capability.value!r} is forbidden to every role"
            + (f" (attempted by {role.value}: {context})" if context else "")
        )
    if not can(role, capability):
        held = ", ".join(sorted(item.value for item in ROLE_CAPABILITIES.get(role, ())))
        raise RoleContractViolation(
            f"role {role.value!r} does not hold capability {capability.value!r}"
            + (f" while attempting: {context}" if context else "")
            + f"; {role.value} holds: [{held}]"
        )


def require_engine(role: AgentRole, *, context: str = "") -> None:
    """Assert that ``role`` is the SOP Engine."""
    if role is not AgentRole.SOP_ENGINE:
        raise RoleContractViolation(
            f"operation requires role {AgentRole.SOP_ENGINE.value!r}, got {role.value!r}"
            + (f" while attempting: {context}" if context else "")
        )


# ---------------------------------------------------------------------------
# I1 -- status authority
# ---------------------------------------------------------------------------

#: Entity collection -> capability required to write its ``status`` field.
STATUS_AUTHORITY: dict[str, Capability] = {
    "tasks": Capability.TRANSITION_TASK,
    "step_runs": Capability.TRANSITION_STEP,
    "attempts": Capability.TRANSITION_ATTEMPT,
    "sop_runs": Capability.TRANSITION_RUN,
    "handoffs": Capability.TRANSITION_HANDOFF,
    "reviews": Capability.TRANSITION_REVIEW,
}


def apply_status(
    entity: object,
    *,
    kind: str,
    value: object,
    actor: AgentRole = AgentRole.SOP_ENGINE,
) -> None:
    """The only sanctioned way to write a SOP status field.

    Args:
        entity: the model object being mutated.
        kind:   the store collection name (``tasks`` / ``step_runs`` / ...).
        value:  the new status value.
        actor:  who is performing the write.  Defaults to the Engine; any
                agent passing itself raises :class:`RoleContractViolation`.
    """
    try:
        capability = STATUS_AUTHORITY[kind]
    except KeyError:
        raise RoleContractViolation(
            f"unknown status collection {kind!r}; "
            f"known: {sorted(STATUS_AUTHORITY)}"
        ) from None
    assert_capability(
        actor,
        capability,
        context=f"write {kind}.status = {getattr(value, 'value', value)!r}",
    )
    entity.status = value  # type: ignore[attr-defined]


#: Task statuses that end a task.  ``ACCEPTED`` is deliberately NOT terminal:
#: it means "the engine accepted the produced artifact", the task is still
#: subject to the review gate.  Only the Engine may reach a terminal state.
TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.FAILED})

TERMINAL_STEP_STATUSES: frozenset[StepStatus] = frozenset(
    {StepStatus.COMPLETED, StepStatus.FAILED}
)

TERMINAL_RUN_STATUSES: frozenset[SopRunStatus] = frozenset(
    {SopRunStatus.COMPLETED, SopRunStatus.FAILED, SopRunStatus.CANCELLED}
)

#: Statuses an agent is never allowed to put a task into on its own.
AGENT_FORBIDDEN_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.ACCEPTED, TaskStatus.FAILED}
)


def non_terminal_task_statuses() -> frozenset[TaskStatus]:
    """Task statuses that do NOT end the task (accepted is not terminal)."""
    return frozenset(TaskStatus) - TERMINAL_TASK_STATUSES


# ---------------------------------------------------------------------------
# I6 -- explicit rework / failure routing table (Engine owned)
# ---------------------------------------------------------------------------


class ReviewVerdict(str, Enum):
    """Vocabulary the Reviewer (Codex) is allowed to emit."""

    PASS = "PASS"
    REWORK = "REWORK"
    PLAN_INVALID = "PLAN_INVALID"


class EngineCondition(str, Enum):
    """Conditions detected by the Engine itself, not by the Reviewer."""

    EXECUTION_ERROR = "execution_error"
    PLAN_OBSOLETE = "plan_obsolete"
    EVIDENCE_INCOMPLETE = "evidence_incomplete"


class RouteTarget(str, Enum):
    """Where the Engine sends the run after a verdict or condition."""

    ADVANCE = "advance"              # accept and move to the next stage
    RERUN_EXECUTE = "rerun_execute"  # back to the executor with the same plan
    RETURN_TO_PLAN = "return_to_plan"  # back to the planner
    FAIL_RUN = "fail_run"            # terminal, no further routing
    HUMAN = "human"                  # policy gate needs a human decision


#: THE routing table.  Single source of truth: no agent and no test script may
#: invent a destination that is not in this map.
REVIEW_ROUTING: dict[str, RouteTarget] = {
    ReviewVerdict.PASS.value: RouteTarget.ADVANCE,
    ReviewVerdict.REWORK.value: RouteTarget.RERUN_EXECUTE,
    ReviewVerdict.PLAN_INVALID.value: RouteTarget.RETURN_TO_PLAN,
    EngineCondition.EXECUTION_ERROR.value: RouteTarget.RERUN_EXECUTE,
    EngineCondition.PLAN_OBSOLETE.value: RouteTarget.RETURN_TO_PLAN,
    EngineCondition.EVIDENCE_INCOMPLETE.value: RouteTarget.HUMAN,
}


def resolve_route(
    signal: ReviewVerdict | EngineCondition | str,
    *,
    actor: AgentRole = AgentRole.SOP_ENGINE,
) -> RouteTarget:
    """Resolve a verdict/condition to a route.  Engine-only operation."""
    require_engine(actor, context="resolve_route")
    key = getattr(signal, "value", signal)
    try:
        return REVIEW_ROUTING[str(key)]
    except KeyError:
        raise RoleContractViolation(
            f"no route defined for signal {key!r}; "
            f"known signals: {sorted(REVIEW_ROUTING)}"
        ) from None


# ---------------------------------------------------------------------------
# I5 / stage contracts -- PLAN -> EXECUTE -> ARTIFACT -> REVIEW
# ---------------------------------------------------------------------------


class Stage(str, Enum):
    PLAN = "plan"
    EXECUTE = "execute"
    REVIEW = "review"


STAGE_OWNER: dict[Stage, AgentRole] = {
    Stage.PLAN: AgentRole.CLAUDE,
    Stage.EXECUTE: AgentRole.DSH,
    Stage.REVIEW: AgentRole.CODEX,
}

STAGE_OUTPUT_ARTIFACT_TYPES: dict[Stage, frozenset[ArtifactType]] = {
    Stage.PLAN: frozenset({ArtifactType.PLAN}),
    Stage.EXECUTE: frozenset(
        {
            ArtifactType.IMPLEMENTATION,
            ArtifactType.DIFF,
            ArtifactType.TEST_REPORT,
            ArtifactType.FILE,
            ArtifactType.TEXT,
            ArtifactType.JSON,
        }
    ),
    Stage.REVIEW: frozenset({ArtifactType.REVIEW_REPORT}),
}

#: Stages whose output must be present as an artifact before the next stage
#: may start.  Artifact, not chat history, is the fact carrier (I5).
STAGE_REQUIRES_ARTIFACT: dict[Stage, bool] = {
    Stage.PLAN: True,
    Stage.EXECUTE: True,
    Stage.REVIEW: True,
}

#: The review stage is only legitimate when these evidence kinds were handed
#: over as artifacts.  A review without them is not a review.
REVIEW_REQUIRED_EVIDENCE: frozenset[ArtifactType] = frozenset(
    {
        ArtifactType.DIFF,
        ArtifactType.TEST_REPORT,
    }
)


def validate_stage_output(
    stage: Stage,
    *,
    artifact_type: ArtifactType | None,
    producer: AgentRole,
) -> None:
    """Assert that ``producer`` is allowed to emit ``artifact_type`` for ``stage``."""
    owner = STAGE_OWNER[stage]
    if producer is not owner:
        raise RoleContractViolation(
            f"stage {stage.value!r} is owned by {owner.value!r}; "
            f"{producer.value!r} may not produce its output"
        )
    allowed = STAGE_OUTPUT_ARTIFACT_TYPES[stage]
    if artifact_type is not None and artifact_type not in allowed:
        raise RoleContractViolation(
            f"stage {stage.value!r} may not emit artifact type "
            f"{artifact_type.value!r}; allowed: "
            f"{sorted(item.value for item in allowed)}"
        )


def missing_review_evidence(available: frozenset[ArtifactType]) -> frozenset[ArtifactType]:
    """Evidence kinds still missing before a review may count as complete."""
    return REVIEW_REQUIRED_EVIDENCE - frozenset(available)


# ---------------------------------------------------------------------------
# Static source audit rules (consumed by tests/workbench/test_role_contract.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRule:
    """A greppable rule describing a forbidden source pattern."""

    rule_id: str
    description: str
    roots: tuple[str, ...]
    pattern: str
    severity: str  # "blocker" | "major" | "minor"


SOURCE_RULES: tuple[SourceRule, ...] = (
    SourceRule(
        rule_id="RC-1",
        description="An agent adapter must not write SOP status fields. "
        "(AgentStatus is the adapter's own process liveness, not SOP state.)",
        roots=("workbench/backend/agents",),
        pattern=r"^\s*\w+\.(?:status|task_status|run_status|step_status)\s*=(?!\s*AgentStatus)",
        severity="blocker",
    ),
    SourceRule(
        rule_id="RC-2",
        description="An agent adapter must not declare a task complete.",
        roots=("workbench/backend/agents",),
        pattern=r"Task completed successfully|task completed|DeepSeek Harness task completed",
        severity="blocker",
    ),
    SourceRule(
        rule_id="RC-3",
        description="An agent adapter must not import the workflow engine "
        "(SubagentRunner is a stateless ABC, so it is exempt).",
        roots=("workbench/backend/agents",),
        pattern=r"from\s+(?:\.\.|workbench\.backend\.)\s*workflow\.engine\s+import(?!\s+SubagentRunner)",
        severity="blocker",
    ),
    SourceRule(
        rule_id="RC-4",
        description="An agent adapter must not self-verify its own completion claim.",
        roots=("workbench/backend/agents",),
        pattern=r"verify_before_done|verify_completion",
        severity="major",
    ),
    SourceRule(
        rule_id="RC-5",
        description="No component outside workflow/ may drive a task to a "
        "terminal state; that is the Engine's sole authority.",
        roots=("workbench/backend/bridge",),
        pattern=r'^\s*"status":\s*TaskStatus\.(?:COMPLETED|FAILED)',
        severity="blocker",
    ),
    SourceRule(
        rule_id="RC-6",
        description="The HTTP layer must not decide a task terminal state on its own "
        "authority; terminal writes go through workflow.run_relay "
        "(which applies them as the Engine).",
        roots=("workbench/backend/main.py",),
        pattern=r"task\.status\s*=\s*TaskStatus\.(?:COMPLETED|FAILED|CANCELLED)",
        severity="blocker",
    ),
)


@dataclass(frozen=True)
class ContractViolation:
    """A known, un-fixed breach of the role contract.

    Every entry is debt.  The audit test asserts that the live violation set
    equals this register, so fixing one without updating the register fails the
    build (good) and introducing a new one fails the build (also good).

    ``routed_to`` names the session that owns the fix.  Role-contract work
    stops at detection + registration; the owning session decides the cure.
    """

    rule_id: str
    path: str
    line: int
    why_open: str
    routed_to: str

    @property
    def identity(self) -> tuple[str, str, int]:
        return (self.rule_id, self.path, self.line)


#: Sessions referenced by the register.
SESSION_COMMIT_GATE = "会话: Commit Gate / 冻结解冻决策"
SESSION_LEGACY_BRIDGE = "会话: legacy bridge 与 Agent 运行时收敛"
SESSION_MAINLINE_E2E = "会话: 主链接线与 Final E2E"

_FROZEN = "inside the Commit A frozen set (21 files)"
_PLANNER_CLAIM = "Planner (Claude) declares the task complete; terminality belongs to the Engine"
_EXECUTOR_CLAIM = "Executor (DSH) declares the task complete; DSH owns no review right"
_SELF_VERIFY = "agent verifies its own completion claim instead of letting Codex review it"
_SECOND_STATE_MACHINE = (
    "bridge/ drives task status next to WorkflowEngine; collapsing it changes "
    "the legacy /api/tasks path and needs its own regression pass"
)

KNOWN_VIOLATIONS: frozenset[ContractViolation] = frozenset(
    {
        # ---- RC-2: an agent declares the task complete --------------------
        ContractViolation(
            rule_id="RC-2",
            path="workbench/backend/agents/claude_adapter.py",
            line=282,
            why_open=f"{_FROZEN}; {_PLANNER_CLAIM}",
            routed_to=SESSION_COMMIT_GATE,
        ),
        ContractViolation(
            rule_id="RC-2",
            path="workbench/backend/agents/claude_adapter.py",
            line=342,
            why_open=f"{_FROZEN}; {_PLANNER_CLAIM} (second code path)",
            routed_to=SESSION_COMMIT_GATE,
        ),
        ContractViolation(
            rule_id="RC-2",
            path="workbench/backend/agents/dsh_adapter.py",
            line=205,
            why_open=f"{_FROZEN}; {_EXECUTOR_CLAIM}",
            routed_to=SESSION_COMMIT_GATE,
        ),
        # ---- RC-4: an agent self-verifies its own completion --------------
        ContractViolation(
            rule_id="RC-4",
            path="workbench/backend/agents/base.py",
            line=174,
            why_open=f"{_FROZEN}; verify_completion() grants self-acceptance to "
            "every role through the shared base class",
            routed_to=SESSION_COMMIT_GATE,
        ),
        ContractViolation(
            rule_id="RC-4",
            path="workbench/backend/agents/claude_adapter.py",
            line=325,
            why_open=f"{_FROZEN}; {_SELF_VERIFY}",
            routed_to=SESSION_COMMIT_GATE,
        ),
        ContractViolation(
            rule_id="RC-4",
            path="workbench/backend/agents/claude_adapter.py",
            line=372,
            why_open=f"{_FROZEN}; {_SELF_VERIFY}",
            routed_to=SESSION_COMMIT_GATE,
        ),
        ContractViolation(
            rule_id="RC-4",
            path="workbench/backend/agents/claude_adapter.py",
            line=379,
            why_open=f"{_FROZEN}; {_SELF_VERIFY}",
            routed_to=SESSION_COMMIT_GATE,
        ),
        ContractViolation(
            rule_id="RC-4",
            path="workbench/backend/agents/codex_adapter.py",
            line=255,
            why_open=f"{_SELF_VERIFY}; removing it changes the legacy run flow "
            "and is not frozen, but needs a behaviour regression pass",
            routed_to=SESSION_LEGACY_BRIDGE,
        ),
        ContractViolation(
            rule_id="RC-4",
            path="workbench/backend/agents/codex_adapter.py",
            line=369,
            why_open=f"{_SELF_VERIFY}; same as line 255",
            routed_to=SESSION_LEGACY_BRIDGE,
        ),
        ContractViolation(
            rule_id="RC-4",
            path="workbench/backend/agents/codex_adapter.py",
            line=376,
            why_open=f"{_SELF_VERIFY}; same as line 255",
            routed_to=SESSION_LEGACY_BRIDGE,
        ),
        # ---- RC-5: a second task state machine ----------------------------
        ContractViolation(
            rule_id="RC-5",
            path="workbench/backend/bridge/service.py",
            line=112,
            why_open=_SECOND_STATE_MACHINE,
            routed_to=SESSION_LEGACY_BRIDGE,
        ),
        ContractViolation(
            rule_id="RC-5",
            path="workbench/backend/bridge/service.py",
            line=122,
            why_open=_SECOND_STATE_MACHINE,
            routed_to=SESSION_LEGACY_BRIDGE,
        ),
        ContractViolation(
            rule_id="RC-5",
            path="workbench/backend/bridge/service.py",
            line=132,
            why_open=_SECOND_STATE_MACHINE,
            routed_to=SESSION_LEGACY_BRIDGE,
        ),
        # RC-6 retired 2026-09-07: the HTTP layer no longer decides a task
        # terminal state; it relays through workflow.run_relay, which writes
        # under Engine authority.  Re-register it if a terminal assignment
        # ever reappears in main.py.
    }
)

CONTRACT_DEBT_REGISTER: tuple[ContractViolation, ...] = tuple(
    sorted(KNOWN_VIOLATIONS, key=lambda item: (item.rule_id, item.path, item.line))
)
