"""Deterministic contract tests for Engine-owned review outcome routing."""

from __future__ import annotations

import json

import pytest

from workbench.backend.domain.models import (
    Artifact,
    ArtifactType,
    ContextPackage,
    Goal,
    HandoffMessageType,
    SopDefinition,
    StageDefinition,
    StepDefinition,
    SubagentAssignment,
    Task,
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
from workbench.backend.workflow.orchestrator import AutoOrchestrator


class AcceptAll(AcceptanceValidator):
    async def validate(self, *, task: Task, artifact: Artifact) -> ValidationResult:
        return ValidationResult(
            id=f"validation-{task.id}",
            task_id=task.id,
            artifact_id=artifact.id,
            validator_id="test",
            status=ValidationStatus.ACCEPTED,
        )


class ReviewLoopRunner:
    """Return a fixed plan/diff+test/review sequence (contract v2)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.dsh_contexts: list[ContextPackage] = []
        self._review_count = 0

    async def execute(
        self, *, task: Task, assignment, context: ContextPackage
    ) -> RuntimeExecutionResult:
        self.calls.append(task.role_id)
        if task.role_id == "claude":
            return RuntimeExecutionResult(
                artifacts=[
                    Artifact(
                        id=f"artifact-{task.id}",
                        task_id=task.id,
                        type=ArtifactType.PLAN,
                        content=(
                            f"PLAN v{len([role for role in self.calls if role == 'claude'])}"
                        ),
                    )
                ]
            )
        if task.role_id == "dsh":
            self.dsh_contexts.append(context)
            return RuntimeExecutionResult(
                artifacts=[
                    Artifact(
                        id=f"diff-{task.id}",
                        task_id=task.id,
                        type=ArtifactType.DIFF,
                        content="+implementation",
                    ),
                    Artifact(
                        id=f"test-{task.id}",
                        task_id=task.id,
                        type=ArtifactType.TEST_REPORT,
                        content="PASS",
                    ),
                ]
            )

        self._review_count += 1
        verdicts = (
            ("PLAN_INVALID", "plan targets the wrong module"),
            ("REWORK", "implementation still has a failing edge case"),
            ("PASS", "all required checks are green"),
        )
        result, blocking = verdicts[self._review_count - 1]
        return RuntimeExecutionResult(
            artifacts=[
                Artifact(
                    id=f"artifact-{task.id}",
                    task_id=task.id,
                    type=ArtifactType.REVIEW_REPORT,
                    content=json.dumps(
                        {
                            "result": result,
                            "blocking": [blocking] if result != "PASS" else [],
                            "non_blocking": [],
                            "evidence": ["deterministic test"],
                        }
                    ),
                )
            ]
        )


def _sop() -> tuple[Goal, SopDefinition]:
    goal = Goal(id="goal-routing", project_id="project-routing", description="ship it")
    return goal, SopDefinition(
        id="sop-routing",
        name="plan execute review",
        stages=[
            StageDefinition(
                id="stage-1",
                name="main",
                steps=[
                    StepDefinition(
                        id="plan",
                        name="Plan",
                        role_id="claude",
                        output_type=ArtifactType.PLAN,
                        handoff_to="execute",
                        retry_limit=2,
                    ),
                    StepDefinition(
                        id="execute",
                        name="Execute",
                        role_id="dsh",
                        output_type=ArtifactType.IMPLEMENTATION,
                        depends_on=["plan"],
                        handoff_to="review",
                        retry_limit=2,
                    ),
                    StepDefinition(
                        id="review",
                        name="Review",
                        role_id="codex",
                        output_type=ArtifactType.REVIEW_REPORT,
                        depends_on=["execute"],
                    ),
                ],
            )
        ],
    )


@pytest.mark.asyncio
async def test_auto_orchestrator_routes_review_outcomes_without_manual_handoffs(tmp_path):
    goal, sop = _sop()
    store = JsonWorkflowStore(tmp_path / "state.json", tmp_path / "events.jsonl")
    runner = ReviewLoopRunner()
    engine = WorkflowEngine(
        store=store,
        runners={"claude": runner, "dsh": runner, "codex": runner},
        validator=AcceptAll(),
    )
    run = engine.start_sop_run(goal=goal, sop=sop)

    def assignment(task: Task, step: StepDefinition) -> SubagentAssignment:
        return SubagentAssignment(
            id=f"assignment-{task.id}",
            task_id=task.id,
            role_id=step.role_id,
            agent_instance_id=f"agent-{step.role_id}",
            runtime_id=step.role_id,
        )

    def context(task: Task, step: StepDefinition) -> ContextPackage:
        return ContextPackage(
            id=f"context-{task.id}",
            task_id=task.id,
            goal_summary=goal.description,
            instructions=step.instructions or step.name,
        )

    results = await AutoOrchestrator(engine).run_until_gate(
        run.id,
        resolve_assignment=assignment,
        build_context=context,
    )

    assert [result.task.role_id for result in results] == [
        "claude",
        "dsh",
        "codex",
        "claude",
        "dsh",
        "codex",
        "dsh",
        "codex",
    ]
    assert store.get_entity("sop_runs", run.id)["status"] == "completed"
    events = store.replay_events(run.id)
    completed_events = [index for index, event in enumerate(events) if event.event_type == "sop_completed"]
    assert completed_events == [len(events) - 1]

    route_handoffs = [
        item
        for item in store.list_entities("handoffs")
        if item["message_type"] in {
            HandoffMessageType.PLAN_INVALID.value,
            HandoffMessageType.REWORK.value,
        }
    ]
    assert [item["message_type"] for item in route_handoffs] == [
        HandoffMessageType.PLAN_INVALID.value,
        HandoffMessageType.REWORK.value,
    ]
    assert all(item["status"] == "accepted" for item in route_handoffs)
    assert all(item["brief"] for item in route_handoffs)
    assert len({item["correlation_id"] for item in route_handoffs}) == 1

    step_runs = {
        item["step_id"]: item for item in store.list_entities("step_runs")
    }
    assert step_runs["plan"]["rework_count"] == 1
    assert step_runs["execute"]["rework_count"] == 1

    tasks = store.list_entities("tasks")
    plan_tasks = [item for item in tasks if item["role_id"] == "claude"]
    execute_tasks = [item for item in tasks if item["role_id"] == "dsh"]
    assert len(plan_tasks) == 2
    assert len(execute_tasks) == 3
    by_id = {item["id"]: item for item in tasks}
    plan_retry = next(item for item in plan_tasks if item["retry_of"])
    execute_retries = [item for item in execute_tasks if item["retry_of"]]
    assert plan_retry["retry_of"] in by_id
    assert len(execute_retries) == 2
    assert {item["retry_of"] for item in execute_retries} == {
        execute_tasks[0]["id"]
        if execute_tasks[0]["retry_of"] is None
        else next(
            item["id"] for item in execute_tasks if item["retry_of"] is None
        ),
        execute_retries[0]["id"],
    }

    # The route handoff carries the structured review report into the next DSH attempt.
    assert any(
        "failing edge case" in " ".join(
            store.get_entity("artifacts", artifact_id).get("content", "")
            for artifact_id in context.artifact_ids
        )
        for context in runner.dsh_contexts[1:]
    )


def test_rework_limit_fails_run_without_creating_another_handoff(tmp_path):
    from workbench.backend.domain.models import (
        Review,
        ReviewStatus,
        SopRunStatus,
        StepRun,
        StepStatus,
        TaskStatus,
    )
    from workbench.backend.workflow.role_contract import RouteTarget

    goal, sop = _sop()
    execute = sop.stages[0].steps[1].model_copy(update={"retry_limit": 1})
    sop = sop.model_copy(
        update={
            "stages": [
                sop.stages[0].model_copy(
                    update={
                        "steps": [
                            sop.stages[0].steps[0],
                            execute,
                            *sop.stages[0].steps[2:],
                        ]
                    }
                )
            ]
        }
    )
    store = JsonWorkflowStore(tmp_path / "state.json", tmp_path / "events.jsonl")
    engine = WorkflowEngine(
        store=store,
        runners={
            "claude": ReviewLoopRunner(),
            "dsh": ReviewLoopRunner(),
            "codex": ReviewLoopRunner(),
        },
        validator=AcceptAll(),
    )
    run = engine.start_sop_run(goal=goal, sop=sop)

    execute_step = store.get_entity("step_runs", f"{run.id}:execute")
    execute_step["status"] = StepStatus.READY.value
    store.save_entity("step_runs", execute_step)
    execution_task = engine.create_task(f"{run.id}:execute", role_id="dsh")
    execution_task.status = TaskStatus.ACCEPTED
    store.save_entity("tasks", execution_task)

    review_step = StepRun.model_validate(
        store.get_entity("step_runs", f"{run.id}:review")
    )
    review_step.status = StepStatus.COMPLETED
    store.save_entity("step_runs", review_step)

    def add_review(index: int) -> str:
        review_task = Task(
            id=f"review-task-{index}",
            step_run_id=f"{run.id}:review",
            role_id="codex",
            status=TaskStatus.ACCEPTED,
        )
        artifact = Artifact(
            id=f"review-artifact-{index}",
            task_id=review_task.id,
            type=ArtifactType.REVIEW_REPORT,
            content=json.dumps(
                {"result": "REWORK", "blocking": [f"issue-{index}"], "evidence": []}
            ),
        )
        review = Review(
            id=f"review-{index}",
            task_id=review_task.id,
            artifact_id=artifact.id,
            reviewer_role_id="codex",
            reviewed_task_id=execution_task.id,
            status=ReviewStatus.PENDING,
        )
        store.save_entity("tasks", review_task)
        store.save_entity("artifacts", artifact)
        store.save_entity("reviews", review)
        return review.id

    first = engine.apply_review_outcome(add_review(1), verdict="REWORK")
    assert first.route is RouteTarget.RERUN_EXECUTE
    route_handoff_count = len(
        [item for item in store.list_entities("handoffs") if item.get("message_type") == "rework"]
    )

    second = engine.apply_review_outcome(add_review(2), verdict="REWORK")
    assert second.route is RouteTarget.FAIL_RUN
    assert store.get_entity("sop_runs", run.id)["status"] == SopRunStatus.FAILED.value
    assert store.get_entity("step_runs", f"{run.id}:execute")["status"] == StepStatus.FAILED.value
    assert len(
        [item for item in store.list_entities("handoffs") if item.get("message_type") == "rework"]
    ) == route_handoff_count
    assert any(event.event_type == "rework_exhausted" for event in store.replay_events(run.id))


def test_review_verdict_mismatch_is_rejected(tmp_path):
    """The caller cannot override a different result stored by Codex."""
    from workbench.backend.domain.models import (
        Review,
        ReviewStatus,
        StepRun,
        StepStatus,
        TaskStatus,
    )

    store = JsonWorkflowStore(tmp_path / "state.json", tmp_path / "events.jsonl")
    engine = WorkflowEngine(
        store=store,
        runners={
            "claude": ReviewLoopRunner(),
            "dsh": ReviewLoopRunner(),
            "codex": ReviewLoopRunner(),
        },
        validator=AcceptAll(),
    )
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)

    # Minimal persisted review fixture; the artifact says PLAN_INVALID while the
    # caller attempts to route it as PASS.
    task = Task(
        id="review-task",
        step_run_id=f"{run.id}:review",
        role_id="codex",
        status=TaskStatus.ACCEPTED,
    )
    artifact = Artifact(
        id="review-artifact",
        task_id=task.id,
        type=ArtifactType.REVIEW_REPORT,
        content=json.dumps(
            {"result": "PLAN_INVALID", "blocking": ["bad plan"], "evidence": []}
        ),
    )
    review = Review(
        id="review-1",
        task_id=task.id,
        artifact_id=artifact.id,
        reviewer_role_id="codex",
        status=ReviewStatus.PENDING,
    )
    store.save_entity("tasks", task)
    store.save_entity("artifacts", artifact)
    store.save_entity("reviews", review)
    store.save_entity(
        "step_runs",
        StepRun(
            id=f"{run.id}:review",
            sop_run_id=run.id,
            step_id="review",
            status=StepStatus.WAITING_REVIEW,
        ),
    )

    with pytest.raises(WorkflowEngineError, match="does not match"):
        engine.apply_review_outcome("review-1", verdict="PASS")
