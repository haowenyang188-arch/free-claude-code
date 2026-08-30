"""Phase 2 (Runtime/Runner Binding): ExecutionAdapter contract v2 + multi-runtime.

Verifies:
- create_runners fake mode returns claude/dsh/codex/default adapters
- executor adapter produces DIFF + TEST_REPORT as two DISTINCT artifacts
- Engine resolves adapters via assignment.runtime_id / RoleBinding
- Engine stamps task_id / attempt_id / producer_step_run_id / schema_version
  on EVERY returned artifact
- legacy single-runner path still works
- Engine requires either runner or runners
"""

from __future__ import annotations

import pytest

from workbench.backend.domain.models import (
    Artifact,
    ArtifactType,
    Attempt,
    ContextPackage,
    Goal,
    Review,
    SopDefinition,
    StageDefinition,
    StepDefinition,
    SubagentAssignment,
    Task,
    TaskStatus,
    ValidationResult,
    ValidationStatus,
)
from workbench.backend.persistence.store import JsonWorkflowStore
from workbench.backend.workflow.adapter import (
    ExecutionAdapter,
    RuntimeExecutionResult,
)
from workbench.backend.workflow.engine import (
    AcceptanceValidator,
    SubagentRunner,
    WorkflowEngine,
)
from workbench.backend.workflow.runner_factory import create_runners


class AcceptAll(AcceptanceValidator):
    async def validate(self, *, task, artifact) -> ValidationResult:
        return ValidationResult(
            id=f"v-{task.id}",
            task_id=task.id,
            artifact_id=artifact.id,
            validator_id="test",
            status=ValidationStatus.ACCEPTED,
        )


class SingleRunner(SubagentRunner):
    async def execute(self, *, task, assignment, context) -> Artifact:
        return Artifact(
            id=f"a-{task.id}",
            task_id=task.id,
            type=ArtifactType.TEXT,
            content="legacy single",
        )


def _sop() -> tuple[Goal, SopDefinition]:
    goal = Goal(id="g-multi", project_id="p", description="multi-runtime")
    sop = SopDefinition(
        id="sop-multi",
        name="plan execute review",
        stages=[
            StageDefinition(
                id="s1",
                name="main",
                steps=[
                    StepDefinition(
                        id="plan",
                        name="Plan",
                        role_id="planner",
                        output_type=ArtifactType.PLAN,
                        handoff_to="execute",
                    ),
                    StepDefinition(
                        id="execute",
                        name="Execute",
                        role_id="executor",
                        output_type=ArtifactType.IMPLEMENTATION,
                        depends_on=["plan"],
                        handoff_to="review",
                    ),
                    StepDefinition(
                        id="review",
                        name="Review",
                        role_id="reviewer",
                        output_type=ArtifactType.REVIEW_REPORT,
                        depends_on=["execute"],
                    ),
                ],
            )
        ],
    )
    return goal, sop


def _role_runtime(role_id: str) -> str:
    return {"planner": "claude", "executor": "dsh", "reviewer": "codex"}.get(
        role_id, "default"
    )


def _resolvers():
    def resolve_assignment(task, step) -> SubagentAssignment:
        return SubagentAssignment(
            id=f"assign-{task.id}",
            task_id=task.id,
            role_id=task.role_id,
            agent_instance_id="agent",
            runtime_id=_role_runtime(task.role_id),
        )

    def build_context(task, step) -> ContextPackage:
        return ContextPackage(
            id=f"ctx-{task.id}",
            task_id=task.id,
            goal_summary="multi-runtime",
            instructions=step.instructions or step.name,
        )

    return resolve_assignment, build_context


def test_create_runners_fake_mode_keys():
    runners = create_runners(mode="fake")
    assert set(runners) >= {"claude", "dsh", "codex", "default"}


def test_fake_adapters_implement_protocol():
    runners = create_runners(mode="fake")
    for adapter in runners.values():
        assert isinstance(adapter, ExecutionAdapter)


@pytest.mark.asyncio
async def test_executor_adapter_produces_diff_and_test_report():
    runners = create_runners(mode="fake")
    task = Task(id="t1", step_run_id="s1", role_id="executor")
    assignment = SubagentAssignment(
        id="a1", task_id="t1", role_id="executor", agent_instance_id="x", runtime_id="dsh"
    )
    context = ContextPackage(
        id="c1", task_id="t1", goal_summary="g", instructions="i"
    )
    result = await runners["dsh"].execute(
        task=task, assignment=assignment, context=context
    )
    assert isinstance(result, RuntimeExecutionResult)
    types = {a.type for a in result.artifacts}
    assert ArtifactType.DIFF in types
    assert ArtifactType.TEST_REPORT in types
    assert len(result.artifacts) == 2
    assert result.session_id


def test_engine_requires_runner_or_runners(tmp_path):
    store = JsonWorkflowStore(tmp_path / "s.json", tmp_path / "e.jsonl")
    with pytest.raises(ValueError, match=r"runner.*runners"):
        WorkflowEngine(store=store, validator=AcceptAll())


@pytest.mark.asyncio
async def test_engine_multi_runtime_full_loop(tmp_path):
    """Full loop over fake adapters: every artifact stamped with attempt_id."""
    store = JsonWorkflowStore(tmp_path / "s.json", tmp_path / "e.jsonl")
    runners = create_runners(mode="fake")
    engine = WorkflowEngine(
        store=store, runners=runners, validator=AcceptAll()
    )
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    from workbench.backend.workflow.orchestrator import AutoOrchestrator

    await AutoOrchestrator(engine).run_until_gate(
        run.id,
        resolve_assignment=_resolvers()[0],
        build_context=_resolvers()[1],
    )

    tasks = store.list_entities("tasks")
    attempts = store.list_entities("attempts")
    artifacts = store.list_entities("artifacts")
    assert len(tasks) == 3
    assert len(attempts) == 3
    assert len(artifacts) == 4  # PLAN + (DIFF, TEST_REPORT) + REVIEW_REPORT

    by_task = {a["task_id"]: a for a in attempts}
    for art in artifacts:
        assert art["attempt_id"] is not None
        assert art["attempt_id"] == by_task[art["task_id"]]["id"]
        assert art["schema_version"] == 2

    executor_artifacts = [
        a for a in artifacts if a["task_id"] in {t["id"] for t in tasks if t["role_id"] == "executor"}
    ]
    executor_types = {a["type"] for a in executor_artifacts}
    assert ArtifactType.DIFF.value in executor_types
    assert ArtifactType.TEST_REPORT.value in executor_types
    # DIFF and TEST_REPORT belong to the SAME attempt
    assert len({a["attempt_id"] for a in executor_artifacts}) == 1

    reviews = store.list_entities("reviews")
    assert len(reviews) == 1
    review = Review.model_validate(reviews[0])
    executor_task = next(t for t in tasks if t["role_id"] == "executor")
    assert review.reviewed_attempt_id == by_task[executor_task["id"]]["id"]


@pytest.mark.asyncio
async def test_engine_legacy_single_runner_still_works(tmp_path):
    store = JsonWorkflowStore(tmp_path / "s.json", tmp_path / "e.jsonl")
    engine = WorkflowEngine(store=store, runner=SingleRunner(), validator=AcceptAll())
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    step_run = engine.ready_step_runs(run.id)[0]
    task = engine.create_task(step_run.id, role_id="planner")
    assignment = SubagentAssignment(
        id=f"a-{task.id}",
        task_id=task.id,
        role_id=task.role_id,
        agent_instance_id="x",
        runtime_id="fake",
    )
    context = ContextPackage(
        id=f"c-{task.id}", task_id=task.id, goal_summary="g", instructions="i"
    )
    result = await engine.execute_task(task.id, assignment=assignment, context=context)
    assert result.task.status is TaskStatus.ACCEPTED
    artifacts = store.list_entities("artifacts")
    assert len(artifacts) == 1
    assert artifacts[0]["attempt_id"] is not None
    assert artifacts[0]["schema_version"] == 2


@pytest.mark.asyncio
async def test_role_binding_resolution_fallback(tmp_path):
    """assignment.runtime_id not in runners -> RoleBinding maps role -> runtime."""
    store = JsonWorkflowStore(tmp_path / "s.json", tmp_path / "e.jsonl")
    runners = create_runners(mode="fake")
    engine = WorkflowEngine(store=store, runners=runners, validator=AcceptAll())
    store.save_entity(
        "role_bindings",
        {
            "id": "rb-1",
            "role_id": "planner",
            "agent_instance_id": "agent-claude",
            "runtime_id": "claude",
        },
    )
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    step_run = engine.ready_step_runs(run.id)[0]
    task = engine.create_task(step_run.id, role_id="planner")
    assignment = SubagentAssignment(
        id=f"a-{task.id}",
        task_id=task.id,
        role_id=task.role_id,
        agent_instance_id="agent-claude",
        runtime_id="missing-in-runners",
    )
    context = ContextPackage(
        id=f"c-{task.id}", task_id=task.id, goal_summary="g", instructions="i"
    )
    result = await engine.execute_task(task.id, assignment=assignment, context=context)
    artifact = store.list_entities("artifacts")[0]
    assert Artifact(**(artifact)).type is ArtifactType.PLAN
    assert result.task.status is TaskStatus.ACCEPTED


@pytest.mark.asyncio
async def test_runner_exception_marks_attempt_failed(tmp_path):
    class Boom:
        async def execute(self, *, task, assignment, context) -> RuntimeExecutionResult:
            raise RuntimeError("boom")

    store = JsonWorkflowStore(tmp_path / "s.json", tmp_path / "e.jsonl")
    engine = WorkflowEngine(
        store=store, runners={"dsh": Boom()}, validator=AcceptAll()
    )
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    step_run = engine.ready_step_runs(run.id)[0]
    task = engine.create_task(step_run.id, role_id="planner")
    assignment = SubagentAssignment(
        id=f"a-{task.id}",
        task_id=task.id,
        role_id=task.role_id,
        agent_instance_id="x",
        runtime_id="dsh",
    )
    context = ContextPackage(
        id=f"c-{task.id}", task_id=task.id, goal_summary="g", instructions="i"
    )
    with pytest.raises(RuntimeError, match="boom"):
        await engine.execute_task(task.id, assignment=assignment, context=context)
    attempt = Attempt.model_validate(store.list_entities("attempts")[0])
    assert attempt.status is TaskStatus.FAILED
