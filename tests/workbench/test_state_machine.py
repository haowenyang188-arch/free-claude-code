"""Phase 4 (Engine State Machine / Recovery) tests.

Covers (execution-side revision #7):
- cancel / pause / resume as ENGINE state transitions (apply_status)
- duplicate execute blocked
- runner / validator exceptions fail task+step+attempt via legal engine paths
- late results after cancel cannot flip CANCELLED back to a live state
- recovery (orphaned tasks, stuck steps) goes through engine state transitions
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from workbench.backend.domain.models import (
    Artifact,
    ArtifactType,
    Attempt,
    ContextPackage,
    Goal,
    Review,
    ReviewStatus,
    SopDefinition,
    SopRun,
    SopRunStatus,
    StageDefinition,
    StepDefinition,
    StepRun,
    StepStatus,
    SubagentAssignment,
    Task,
    TaskStatus,
    ValidationResult,
    ValidationStatus,
)
from workbench.backend.persistence.store import JsonWorkflowStore
from workbench.backend.workflow.adapter import RuntimeExecutionResult
from workbench.backend.workflow.engine import (
    AcceptanceValidator,
    WorkflowEngine,
    WorkflowEngineError,
)
from workbench.backend.workflow.role_contract import (
    AgentRole,
    RoleContractViolation,
    apply_status,
)


class AcceptAll(AcceptanceValidator):
    async def validate(self, *, task, artifact) -> ValidationResult:
        return ValidationResult(
            id=f"v-{task.id}",
            task_id=task.id,
            artifact_id=artifact.id,
            validator_id="test",
            status=ValidationStatus.ACCEPTED,
        )


class BoomRunner:
    """Adapter that always raises."""

    async def execute(self, *, task, assignment, context) -> RuntimeExecutionResult:
        raise RuntimeError("runner boom")


class BoomValidator(AcceptanceValidator):
    async def validate(self, *, task, artifact) -> ValidationResult:
        raise RuntimeError("validator boom")


class PlanAdapter:
    """Deterministic single-step adapter (PLAN only)."""

    async def execute(self, *, task, assignment, context) -> RuntimeExecutionResult:
        return RuntimeExecutionResult(
            artifacts=[
                Artifact(
                    id=f"plan-{task.id}",
                    task_id=task.id,
                    type=ArtifactType.PLAN,
                    content="plan",
                )
            ]
        )


def _sop() -> tuple[Goal, SopDefinition]:
    goal = Goal(id="g-sm", project_id="p", description="state machine")
    sop = SopDefinition(
        id="sop-sm",
        name="plan",
        stages=[
            StageDefinition(
                id="s1",
                name="main",
                steps=[
                    StepDefinition(
                        id="plan",
                        name="Plan",
                        role_id="claude",
                        output_type=ArtifactType.PLAN,
                    )
                ],
            )
        ],
    )
    return goal, sop


def _resolvers():
    def resolve_assignment(task, step) -> SubagentAssignment:
        return SubagentAssignment(
            id=f"assign-{task.id}",
            task_id=task.id,
            role_id=task.role_id,
            agent_instance_id="agent",
            runtime_id="claude",
        )

    def build_context(task, step) -> ContextPackage:
        return ContextPackage(
            id=f"ctx-{task.id}",
            task_id=task.id,
            goal_summary="state machine",
            instructions=step.instructions or step.name,
        )

    return resolve_assignment, build_context


def _engine(tmp_path, adapter, validator=None) -> tuple[WorkflowEngine, JsonWorkflowStore]:
    store = JsonWorkflowStore(tmp_path / "s.json", tmp_path / "e.jsonl")
    engine = WorkflowEngine(
        store=store,
        runners={"claude": adapter},
        validator=validator or AcceptAll(),
    )
    return engine, store


def test_apply_status_blocks_unauthorized_attempt_transition():
    attempt = Attempt(id="a1", task_id="t1", status=TaskStatus.RUNNING)
    with pytest.raises(RoleContractViolation):
        apply_status(
            attempt, kind="attempts", value=TaskStatus.FAILED, actor=AgentRole.CODEX
        )
    apply_status(
        attempt, kind="attempts", value=TaskStatus.FAILED, actor=AgentRole.SOP_ENGINE
    )
    assert attempt.status is TaskStatus.FAILED


@pytest.mark.asyncio
async def test_duplicate_execute_blocked(tmp_path):
    engine, _ = _engine(tmp_path, PlanAdapter())
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    step_run = engine.ready_step_runs(run.id)[0]
    task = engine.create_task(step_run.id, role_id="claude")
    assignment, context = _resolvers()[0](task, sop.stages[0].steps[0]), _resolvers()[1](
        task, sop.stages[0].steps[0]
    )
    result = await engine.execute_task(task.id, assignment=assignment, context=context)
    assert result.task.status is TaskStatus.ACCEPTED
    with pytest.raises(WorkflowEngineError, match="duplicate execute"):
        await engine.execute_task(task.id, assignment=assignment, context=context)


@pytest.mark.asyncio
async def test_runner_exception_fails_via_engine(tmp_path):
    engine, store = _engine(tmp_path, BoomRunner())
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    step_run = engine.ready_step_runs(run.id)[0]
    task = engine.create_task(step_run.id, role_id="claude")
    assignment, context = _resolvers()[0](task, sop.stages[0].steps[0]), _resolvers()[1](
        task, sop.stages[0].steps[0]
    )
    with pytest.raises(RuntimeError, match="runner boom"):
        await engine.execute_task(task.id, assignment=assignment, context=context)
    saved = Task.model_validate(store.get_entity("tasks", task.id))
    assert saved.status is TaskStatus.FAILED
    step_saved = StepRun.model_validate(store.get_entity("step_runs", step_run.id))
    assert step_saved.status is StepStatus.FAILED
    attempt = Attempt.model_validate(store.list_entities("attempts")[0])
    assert attempt.status is TaskStatus.FAILED


@pytest.mark.asyncio
async def test_validator_exception_fails_via_engine(tmp_path):
    engine, store = _engine(tmp_path, PlanAdapter(), validator=BoomValidator())
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    step_run = engine.ready_step_runs(run.id)[0]
    task = engine.create_task(step_run.id, role_id="claude")
    assignment, context = _resolvers()[0](task, sop.stages[0].steps[0]), _resolvers()[1](
        task, sop.stages[0].steps[0]
    )
    with pytest.raises(RuntimeError, match="validator boom"):
        await engine.execute_task(task.id, assignment=assignment, context=context)
    assert Task.model_validate(store.get_entity("tasks", task.id)).status is TaskStatus.FAILED
    assert StepRun.model_validate(store.get_entity("step_runs", step_run.id)).status is StepStatus.FAILED


def test_pause_resume_sop_run(tmp_path):
    engine, store = _engine(tmp_path, PlanAdapter())
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    engine.pause_sop_run(sop_run_id=run.id, reason="halt")
    assert SopRun.model_validate(store.get_entity("sop_runs", run.id)).status is SopRunStatus.PAUSED
    engine.resume_sop_run(sop_run_id=run.id)
    assert SopRun.model_validate(store.get_entity("sop_runs", run.id)).status is SopRunStatus.RUNNING



@pytest.mark.asyncio
async def test_cancel_invalidates_inflight_and_blocks_late_execution(tmp_path):
    engine, store = _engine(tmp_path, PlanAdapter())
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    step_run = engine.ready_step_runs(run.id)[0]

    # Simulate in-flight work: RUNNING step + RUNNING task + RUNNING attempt.
    task = engine.create_task(step_run.id, role_id="claude")
    attempt = engine._current_attempt_for_task(task.id)
    task.status = TaskStatus.RUNNING
    attempt.status = TaskStatus.RUNNING
    attempt.started_at = datetime.now(UTC)
    engine.store.save_entity("tasks", task)
    engine.store.save_entity("attempts", attempt)
    step_run.status = StepStatus.RUNNING
    engine.store.save_entity("step_runs", step_run)

    engine.cancel_sop_run(sop_run_id=run.id, reason="abort")
    assert SopRun.model_validate(store.get_entity("sop_runs", run.id)).status is SopRunStatus.CANCELLED
    assert StepRun.model_validate(store.get_entity("step_runs", step_run.id)).status is StepStatus.BLOCKED
    assert Task.model_validate(store.get_entity("tasks", task.id)).status is TaskStatus.FAILED

    # Late execution attempt must not flip CANCELLED back to live:
    # a fresh PENDING task on a cancelled run is rejected by the run gate.
    pending_task = Task(
        id="late-task", step_run_id=step_run.id, role_id="claude",
        status=TaskStatus.PENDING,
    )
    store.save_entity("tasks", pending_task)
    assignment2 = SubagentAssignment(
        id="late-assign", task_id="late-task", role_id="claude",
        agent_instance_id="a", runtime_id="claude",
    )
    context2 = ContextPackage(
        id="late-ctx", task_id="late-task", goal_summary="g", instructions="i"
    )
    with pytest.raises(WorkflowEngineError, match="is cancelled"):
        await engine.execute_task("late-task", assignment=assignment2, context=context2)
    assert SopRun.model_validate(store.get_entity("sop_runs", run.id)).status is SopRunStatus.CANCELLED


def test_cancel_blocks_late_review_outcome(tmp_path):
    engine, store = _engine(tmp_path, PlanAdapter())
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    engine.cancel_sop_run(sop_run_id=run.id)

    reviewer_task = Task(
        id="rev-task", step_run_id=f"{run.id}:plan", role_id="codex",
        status=TaskStatus.ACCEPTED,
    )
    report = Artifact(
        id="report-1", task_id="rev-task", type=ArtifactType.REVIEW_REPORT,
        content=json.dumps({"result": "PASS", "blocking": [], "evidence": []}),
    )
    review = Review(
        id="rev-1", task_id="rev-task", artifact_id="report-1",
        reviewer_role_id="codex", reviewed_task_id="exec-task",
        status=ReviewStatus.PENDING,
    )
    store.save_entity("tasks", reviewer_task)
    store.save_entity("artifacts", report)
    store.save_entity("reviews", review)
    store.save_entity(
        "step_runs",
        StepRun(id=f"{run.id}:plan", sop_run_id=run.id, step_id="plan"),
    )
    store.save_entity("tasks", Task(id="exec-task", step_run_id=f"{run.id}:plan", role_id="dsh"))

    with pytest.raises(WorkflowEngineError, match="is cancelled"):
        engine.apply_review_outcome("rev-1", verdict="PASS")
    assert SopRun.model_validate(store.get_entity("sop_runs", run.id)).status is SopRunStatus.CANCELLED


def test_recover_orphaned_tasks_via_engine(tmp_path):
    engine, store = _engine(tmp_path, PlanAdapter())
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    step_run = engine.ready_step_runs(run.id)[0]
    task = engine.create_task(step_run.id, role_id="claude")
    task.status = TaskStatus.RUNNING
    store.save_entity("tasks", task)
    # no RUNNING attempt for this task -> orphaned
    recovered = engine.recover_orphaned_tasks()
    assert task.id in recovered
    assert Task.model_validate(store.get_entity("tasks", task.id)).status is TaskStatus.FAILED
    assert StepRun.model_validate(store.get_entity("step_runs", step_run.id)).status is StepStatus.FAILED


def test_recover_stuck_step_runs_via_engine(tmp_path):
    engine, store = _engine(tmp_path, PlanAdapter())
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    step_run = engine.ready_step_runs(run.id)[0]
    task = engine.create_task(step_run.id, role_id="claude")
    task.status = TaskStatus.RUNNING
    store.save_entity("tasks", task)
    attempt = engine._current_attempt_for_task(task.id)
    attempt.status = TaskStatus.RUNNING
    attempt.started_at = datetime.now(UTC) - timedelta(hours=2)
    store.save_entity("attempts", attempt)

    recovered = engine.recover_stuck_step_runs(timeout_seconds=3600)
    assert task.id in recovered
    assert Task.model_validate(store.get_entity("tasks", task.id)).status is TaskStatus.FAILED
    assert StepRun.model_validate(store.get_entity("step_runs", step_run.id)).status is StepStatus.FAILED


@pytest.mark.asyncio
async def test_pause_blocks_execution(tmp_path):
    engine, _ = _engine(tmp_path, PlanAdapter())
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    step_run = engine.ready_step_runs(run.id)[0]
    task = engine.create_task(step_run.id, role_id="claude")
    engine.pause_sop_run(sop_run_id=run.id)
    assignment = SubagentAssignment(
        id="a1", task_id=task.id, role_id="claude", agent_instance_id="a",
        runtime_id="claude",
    )
    context = ContextPackage(
        id="c1", task_id=task.id, goal_summary="g", instructions="i"
    )
    with pytest.raises(WorkflowEngineError, match="paused"):
        await engine.execute_task(task.id, assignment=assignment, context=context)



@pytest.mark.asyncio
async def test_cancel_races_validator_completion(tmp_path):
    """Cancel during validator await: post-validation guard rejects late writes."""
    import asyncio

    engine, store = _engine(tmp_path, PlanAdapter())
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    step_run = engine.ready_step_runs(run.id)[0]
    task = engine.create_task(step_run.id, role_id="claude")

    class RaceValidator(AcceptanceValidator):
        async def validate(self, *, task, artifact) -> ValidationResult:
            await asyncio.sleep(0.05)
            engine.cancel_sop_run(sop_run_id=run.id, reason="race")
            return ValidationResult(
                id=f"v-{task.id}", task_id=task.id, artifact_id=artifact.id,
                validator_id="test", status=ValidationStatus.ACCEPTED,
            )

    engine.validator = RaceValidator()
    assignment = SubagentAssignment(
        id="a1", task_id=task.id, role_id="claude", agent_instance_id="a",
        runtime_id="claude",
    )
    context = ContextPackage(
        id="c1", task_id=task.id, goal_summary="g", instructions="i"
    )
    with pytest.raises(WorkflowEngineError, match="is cancelled"):
        await engine.execute_task(task.id, assignment=assignment, context=context)
    assert SopRun.model_validate(store.get_entity("sop_runs", run.id)).status is SopRunStatus.CANCELLED
    saved = Task.model_validate(store.get_entity("tasks", task.id))
    assert saved.status is not TaskStatus.ACCEPTED
    # The cancelled run must not be flipped by _refresh_run_status later.
    assert SopRun.model_validate(store.get_entity("sop_runs", run.id)).status is SopRunStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_races_runner_exception(tmp_path):
    """Cancel during runner await, then runner raises: cancelled state preserved."""
    import asyncio

    engine, store = _engine(tmp_path, PlanAdapter())
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    step_run = engine.ready_step_runs(run.id)[0]
    task = engine.create_task(step_run.id, role_id="claude")

    class RaceBoom:
        async def execute(self, *, task, assignment, context) -> RuntimeExecutionResult:
            await asyncio.sleep(0.05)
            engine.cancel_sop_run(sop_run_id=run.id, reason="race")
            raise RuntimeError("late boom")

    engine._runners = {"claude": RaceBoom()}
    assignment = SubagentAssignment(
        id="a1", task_id=task.id, role_id="claude", agent_instance_id="a",
        runtime_id="claude",
    )
    context = ContextPackage(
        id="c1", task_id=task.id, goal_summary="g", instructions="i"
    )
    with pytest.raises(RuntimeError, match="late boom"):
        await engine.execute_task(task.id, assignment=assignment, context=context)
    assert SopRun.model_validate(store.get_entity("sop_runs", run.id)).status is SopRunStatus.CANCELLED
    # Step stays BLOCKED (cancellation-invalidated), NOT overwritten to FAILED.
    assert StepRun.model_validate(store.get_entity("step_runs", step_run.id)).status is StepStatus.BLOCKED
