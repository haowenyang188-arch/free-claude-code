"""Role Contract -- executable verification of the frozen role model.

Session 3 completion criterion: the role boundaries must stop being an oral
agreement and become a system contract that code and tests can verify.

Mapping to the five frozen rules:

    SOP_ENGINE  owns every Task / StepRun / SopRun status transition
    CLAUDE      plans only; never reviews, never declares completion
    DSH         executes only; never redefines requirements, never reviews
    CODEX       reviews only; PASS|REJECT, never rewrites the flow state
    Artifact    sole fact carrier between stages
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from workbench.backend.domain.models import (
    ArtifactType,
    SopRunStatus,
    StepStatus,
    Task,
    TaskStatus,
)
from workbench.backend.workflow.role_contract import (
    AGENT_FORBIDDEN_TASK_STATUSES,
    AGENT_ROLES,
    CONTRACT_DEBT_REGISTER,
    FORBIDDEN_CAPABILITIES,
    KNOWN_VIOLATIONS,
    REVIEW_ROUTING,
    ROLE_CAPABILITIES,
    SOURCE_RULES,
    STAGE_OWNER,
    STAGE_OUTPUT_ARTIFACT_TYPES,
    TERMINAL_RUN_STATUSES,
    TERMINAL_STEP_STATUSES,
    TERMINAL_TASK_STATUSES,
    STATUS_AUTHORITY,
    TRANSITION_CAPABILITIES,
    AgentRole,
    Capability,
    EngineCondition,
    ReviewVerdict,
    RoleContractViolation,
    Stage,
    apply_status,
    assert_capability,
    missing_review_evidence,
    non_terminal_task_statuses,
    require_engine,
    resolve_route,
    validate_stage_output,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# frozen role model
# ---------------------------------------------------------------------------


def test_role_model_is_frozen_to_registered_roles() -> None:
    # 2026-09-06 定版：新增第五角色 codex_executor（可写执行者）；
    # dsh 为遗留角色，仅为历史 run 数据兼容保留，新流水线不再分配。
    assert {role.value for role in AgentRole} == {
        "sop_engine",
        "claude",
        "dsh",
        "codex",
        "codex_executor",
    }


def test_agent_roles_exclude_the_engine() -> None:
    assert AgentRole.SOP_ENGINE not in AGENT_ROLES
    assert AGENT_ROLES == frozenset(
        {AgentRole.CLAUDE, AgentRole.DSH, AgentRole.CODEX, AgentRole.CODEX_EXECUTOR}
    )


# ---------------------------------------------------------------------------
# capability matrix
# ---------------------------------------------------------------------------


def test_only_the_engine_holds_transition_capabilities() -> None:
    """I1: no agent may transition SOP state."""
    for role in AGENT_ROLES:
        held = ROLE_CAPABILITIES[role]
        assert held & TRANSITION_CAPABILITIES == frozenset(), (
            f"{role.value} holds transition capabilities: "
            f"{sorted(c.value for c in held & TRANSITION_CAPABILITIES)}"
        )
    assert ROLE_CAPABILITIES[AgentRole.SOP_ENGINE] == TRANSITION_CAPABILITIES


def test_no_role_holds_a_forbidden_capability() -> None:
    """I2/I3/I4: completion, requirement rewrite, flow change and P2P are denied."""
    for role, held in ROLE_CAPABILITIES.items():
        assert held & FORBIDDEN_CAPABILITIES == frozenset(), (
            f"{role.value} was granted a forbidden capability: "
            f"{sorted(c.value for c in held & FORBIDDEN_CAPABILITIES)}"
        )


def test_claude_is_planner_only() -> None:
    held = ROLE_CAPABILITIES[AgentRole.CLAUDE]
    assert Capability.PRODUCE_PLAN in held
    assert Capability.EMIT_REVIEW_VERDICT not in held, "Claude must not review"
    assert Capability.MODIFY_CODE not in held, "Claude must not execute edits"
    assert Capability.RUN_TESTS not in held


def test_dsh_is_executor_only() -> None:
    held = ROLE_CAPABILITIES[AgentRole.DSH]
    assert Capability.MODIFY_CODE in held
    assert Capability.RUN_TESTS in held
    assert Capability.PRODUCE_PLAN not in held, "DSH must not redefine the plan"
    assert Capability.EMIT_REVIEW_VERDICT not in held, "DSH must not self-review"


def test_codex_is_reviewer_only() -> None:
    held = ROLE_CAPABILITIES[AgentRole.CODEX]
    assert Capability.EMIT_REVIEW_VERDICT in held
    assert Capability.MODIFY_CODE not in held
    assert Capability.PRODUCE_PLAN not in held
    for capability in TRANSITION_CAPABILITIES:
        assert capability not in held


def test_every_capability_is_owned_by_at_least_one_role() -> None:
    granted = frozenset().union(*ROLE_CAPABILITIES.values())
    unassigned = {
        capability
        for capability in Capability
        if capability not in granted
        and capability not in FORBIDDEN_CAPABILITIES
    }
    assert not unassigned, f"capabilities granted to nobody: {sorted(c.value for c in unassigned)}"


# ---------------------------------------------------------------------------
# guard behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", sorted(AGENT_ROLES, key=lambda item: item.value))
def test_agents_cannot_write_task_status(role: AgentRole) -> None:
    """The central invariant: an agent writing status must raise."""
    task = Task(id="task-1", step_run_id="step-1", role_id=role.value)
    with pytest.raises(RoleContractViolation) as excinfo:
        apply_status(task, kind="tasks", value=TaskStatus.ACCEPTED, actor=role)
    assert "does not hold capability" in str(excinfo.value)
    assert task.status is not TaskStatus.ACCEPTED


@pytest.mark.parametrize(
    "kind", sorted(STATUS_AUTHORITY, key=lambda item: item)
)
def test_agents_cannot_write_any_status_collection(kind: str) -> None:
    task = Task(id="task-1", step_run_id="step-1", role_id="dsh")
    with pytest.raises(RoleContractViolation):
        apply_status(task, kind=kind, value="whatever", actor=AgentRole.DSH)


def test_engine_may_write_status() -> None:
    task = Task(id="task-1", step_run_id="step-1", role_id="dsh")
    apply_status(task, kind="tasks", value=TaskStatus.RUNNING, actor=AgentRole.SOP_ENGINE)
    assert task.status is TaskStatus.RUNNING


def test_unknown_status_collection_is_rejected() -> None:
    task = Task(id="task-1", step_run_id="step-1", role_id="dsh")
    with pytest.raises(RoleContractViolation):
        apply_status(task, kind="not_a_collection", value="x", actor=AgentRole.SOP_ENGINE)


def test_forbidden_capability_is_denied_even_to_the_engine() -> None:
    with pytest.raises(RoleContractViolation) as excinfo:
        assert_capability(AgentRole.SOP_ENGINE, Capability.DECLARE_COMPLETION)
    assert "forbidden to every role" in str(excinfo.value)


def test_require_engine_rejects_agents() -> None:
    require_engine(AgentRole.SOP_ENGINE)  # does not raise
    for role in AGENT_ROLES:
        with pytest.raises(RoleContractViolation):
            require_engine(role)


# ---------------------------------------------------------------------------
# terminality
# ---------------------------------------------------------------------------


def test_accepted_is_not_a_terminal_task_status() -> None:
    """ACCEPTED means 'the engine accepted the artifact', not 'the task is done'.

    The review gate still owns terminality, so ACCEPTED must never be terminal.
    """
    assert TaskStatus.ACCEPTED not in TERMINAL_TASK_STATUSES
    assert TaskStatus.ACCEPTED in non_terminal_task_statuses()


def test_terminal_status_sets_are_defined() -> None:
    assert TaskStatus.FAILED in TERMINAL_TASK_STATUSES
    assert StepStatus.COMPLETED in TERMINAL_STEP_STATUSES
    assert SopRunStatus.COMPLETED in TERMINAL_RUN_STATUSES


def test_agents_may_never_reach_a_task_status() -> None:
    """Enforced structurally: agents hold no transition capability at all.

    So an agent cannot reach ACCEPTED or FAILED -- or any other task status --
    because the only write path goes through the capability gate.
    """
    for role in AGENT_ROLES:
        assert ROLE_CAPABILITIES[role] & TRANSITION_CAPABILITIES == frozenset()
    assert AGENT_FORBIDDEN_TASK_STATUSES == frozenset(
        {TaskStatus.ACCEPTED, TaskStatus.FAILED}
    )


# ---------------------------------------------------------------------------
# I6 -- explicit routing table
# ---------------------------------------------------------------------------


def test_routing_table_covers_every_signal() -> None:
    expected = {verdict.value for verdict in ReviewVerdict}
    expected |= {condition.value for condition in EngineCondition}
    assert set(REVIEW_ROUTING) == expected


def test_routing_targets() -> None:
    assert resolve_route(ReviewVerdict.PASS).value == "advance"
    assert resolve_route(ReviewVerdict.REWORK).value == "rerun_execute"
    assert resolve_route(ReviewVerdict.PLAN_INVALID).value == "return_to_plan"
    assert resolve_route(EngineCondition.EXECUTION_ERROR).value == "rerun_execute"
    assert resolve_route(EngineCondition.PLAN_OBSOLETE).value == "return_to_plan"


def test_agents_cannot_resolve_routes() -> None:
    """The destination is the Engine's call, never the Reviewer's or a script's."""
    for role in AGENT_ROLES:
        with pytest.raises(RoleContractViolation):
            resolve_route(ReviewVerdict.REWORK, actor=role)


def test_unknown_signal_has_no_route() -> None:
    with pytest.raises(RoleContractViolation):
        resolve_route("MAYBE", actor=AgentRole.SOP_ENGINE)


@pytest.mark.parametrize("verdict", [item.value for item in ReviewVerdict])
def test_every_review_verdict_has_exactly_one_destination(verdict: str) -> None:
    targets = {REVIEW_ROUTING[verdict]}
    assert len(targets) == 1


# ---------------------------------------------------------------------------
# I5 -- stage contracts
# ---------------------------------------------------------------------------


def test_stage_owner_map() -> None:
    assert STAGE_OWNER[Stage.PLAN] is AgentRole.CLAUDE
    assert STAGE_OWNER[Stage.EXECUTE] is AgentRole.DSH
    assert STAGE_OWNER[Stage.REVIEW] is AgentRole.CODEX


def test_reviewer_may_not_emit_planner_output() -> None:
    with pytest.raises(RoleContractViolation):
        validate_stage_output(
            Stage.PLAN, artifact_type=ArtifactType.PLAN, producer=AgentRole.CODEX
        )


def test_executor_may_not_emit_review_output() -> None:
    with pytest.raises(RoleContractViolation):
        validate_stage_output(
            Stage.REVIEW,
            artifact_type=ArtifactType.REVIEW_REPORT,
            producer=AgentRole.DSH,
        )


def test_owner_may_emit_its_own_stage_output() -> None:
    validate_stage_output(
        Stage.PLAN, artifact_type=ArtifactType.PLAN, producer=AgentRole.CLAUDE
    )
    validate_stage_output(
        Stage.EXECUTE, artifact_type=ArtifactType.DIFF, producer=AgentRole.DSH
    )
    validate_stage_output(
        Stage.REVIEW,
        artifact_type=ArtifactType.REVIEW_REPORT,
        producer=AgentRole.CODEX,
    )


def test_stage_output_type_whitelists_are_disjoint_enough() -> None:
    assert ArtifactType.REVIEW_REPORT not in STAGE_OUTPUT_ARTIFACT_TYPES[Stage.EXECUTE]
    assert ArtifactType.PLAN not in STAGE_OUTPUT_ARTIFACT_TYPES[Stage.EXECUTE]


def test_review_requires_diff_and_test_evidence() -> None:
    """A review without a diff and a test report is not a review."""
    complete = frozenset({ArtifactType.DIFF, ArtifactType.TEST_REPORT})
    assert missing_review_evidence(complete) == frozenset()
    assert missing_review_evidence(frozenset({ArtifactType.DIFF})) == frozenset(
        {ArtifactType.TEST_REPORT}
    )
    assert missing_review_evidence(frozenset()) == frozenset(
        {ArtifactType.DIFF, ArtifactType.TEST_REPORT}
    )


# ---------------------------------------------------------------------------
# static source audit
# ---------------------------------------------------------------------------


def _iter_files(rule_roots: tuple[str, ...]):
    for root in rule_roots:
        path = REPO_ROOT / root
        if path.is_file():
            yield path
            continue
        if not path.exists():
            continue
        for candidate in sorted(path.rglob("*.py")):
            yield candidate


def _scan(rule) -> list[tuple[str, int, str]]:
    pattern = re.compile(rule.pattern)
    findings: list[tuple[str, int, str]] = []
    for file_path in _iter_files(rule.roots):
        try:
            text = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                relative = file_path.relative_to(REPO_ROOT).as_posix()
                findings.append((relative, lineno, line.strip()))
    return findings


def test_source_audit_rules_are_well_formed() -> None:
    for rule in SOURCE_RULES:
        assert rule.rule_id
        assert rule.severity in {"blocker", "major", "minor"}
        re.compile(rule.pattern)  # raises when the pattern is invalid


def test_no_unregistered_role_contract_violation() -> None:
    """Any violation not in the debt register fails the build.

    This is what turns the oral agreement into a gate: a new breach cannot be
    merged silently, and removing an old one without updating the register
    fails too (see :func:`test_contract_debt_register_matches_reality`).
    """
    registered = {item.identity for item in KNOWN_VIOLATIONS}
    live: set[tuple[str, str, int]] = set()
    for rule in SOURCE_RULES:
        for relative, lineno, _ in _scan(rule):
            live.add((rule.rule_id, relative, lineno))

    unregistered = live - registered
    assert not unregistered, (
        "new role contract violation(s) detected; add them to "
        "KNOWN_VIOLATIONS with an owning session, or fix them: "
        + ", ".join(f"{rid}@{path}:{line}" for rid, path, line in sorted(unregistered))
    )


def test_contract_debt_register_matches_reality() -> None:
    """A registered violation that no longer reproduces must be retired."""
    live: set[tuple[str, str, int]] = set()
    for rule in SOURCE_RULES:
        for relative, lineno, _ in _scan(rule):
            live.add((rule.rule_id, relative, lineno))

    stale = {item.identity for item in KNOWN_VIOLATIONS} - live
    assert not stale, (
        "contract debt entries no longer reproduce; remove them from "
        "KNOWN_VIOLATIONS: "
        + ", ".join(f"{rid}@{path}:{line}" for rid, path, line in sorted(stale))
    )


def test_every_registered_violation_names_an_owning_session() -> None:
    """Recorded problems must always say which session owns the fix."""
    for item in CONTRACT_DEBT_REGISTER:
        assert item.routed_to.startswith("会话:"), item


def test_debt_register_is_sorted_and_deduplicated() -> None:
    identities = [item.identity for item in CONTRACT_DEBT_REGISTER]
    assert len(identities) == len(set(identities))
    assert identities == sorted(identities)
