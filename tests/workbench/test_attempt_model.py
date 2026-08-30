"""Phase 1 (Domain Contract): Attempt model + lineage fields (schema v2).

Verifies that Attempt is a first-class lineage entity:
- every new Task execution creates an Attempt
- every new Artifact carries attempt_id (engine-owned)
- every new Review carries reviewed_attempt_id
- rework chains link attempts via previous_attempt_id
- legacy records (attempt_id=None) remain readable
"""

from __future__ import annotations

import json

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
)
from workbench.backend.workflow.orchestrator import AutoOrchestrator


class AcceptAll(AcceptanceValidator):
    async def validate(self, *, task, artifact) -> ValidationResult:
        return ValidationResult(
            id=f"v-{task.id}",
            task_id=task.id,
            artifact_id=artifact.id,
            validator_id="test",
            status=ValidationStatus.ACCEPTED,
        )


class PassRunner:
    """Deterministic adapter: plan -> DIFF+TEST_REPORT -> PASS review report."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, *, task, assignment, context) -> RuntimeExecutionResult:
        self.calls.append(task.role_id)
        if task.role_id == "claude":
            return RuntimeExecutionResult(
                artifacts=[
                    Artifact(
                        id=f"a-{task.id}",
                        task_id=task.id,
                        type=ArtifactType.PLAN,
                        content="plan",
                    )
                ]
            )
        if task.role_id == "dsh":
            return RuntimeExecutionResult(
                artifacts=[
                    Artifact(
                        id=f"diff-{task.id}",
                        task_id=task.id,
                        type=ArtifactType.DIFF,
                        content="+feature",
                    ),
                    Artifact(
                        id=f"test-{task.id}",
                        task_id=task.id,
                        type=ArtifactType.TEST_REPORT,
                        content="PASS (5/5)",
                    ),
                ]
            )
        return RuntimeExecutionResult(
            artifacts=[
                Artifact(
                    id=f"a-{task.id}",
                    task_id=task.id,
                    type=ArtifactType.REVIEW_REPORT,
                    content=json.dumps(
                        {
                            "result": "PASS",
                            "blocking": [],
                            "non_blocking": [],
                            "evidence": ["deterministic test"],
                        }
                    ),
                )
            ]
        )


class ReworkRunner:
    """First review REWORK, second review PASS (contract v2)."""

    def __init__(self) -> None:
        self._review_count = 0

    async def execute(self, *, task, assignment, context) -> RuntimeExecutionResult:
        if task.role_id == "claude":
            return RuntimeExecutionResult(
                artifacts=[
                    Artifact(
                        id=f"a-{task.id}",
                        task_id=task.id,
                        type=ArtifactType.PLAN,
                        content="plan",
                    )
                ]
            )
        if task.role_id == "dsh":
            return RuntimeExecutionResult(
                artifacts=[
                    Artifact(
                        id=f"diff-{task.id}",
                        task_id=task.id,
                        type=ArtifactType.DIFF,
                        content="+feature",
                    ),
                    Artifact(
                        id=f"test-{task.id}",
                        task_id=task.id,
                        type=ArtifactType.TEST_REPORT,
                        content="PASS (5/5)",
                    ),
                ]
            )
        self._review_count += 1
        result = "REWORK" if self._review_count == 1 else "PASS"
        return RuntimeExecutionResult(
            artifacts=[
                Artifact(
                    id=f"a-{task.id}",
                    task_id=task.id,
                    type=ArtifactType.REVIEW_REPORT,
                    content=json.dumps(
                        {
                            "result": result,
                            "blocking": ["fix the edge case"]
                            if result != "PASS"
                            else [],
                            "non_blocking": [],
                            "evidence": ["deterministic test"],
                        }
                    ),
                )
            ]
        )


def _sop() -> tuple[Goal, SopDefinition]:
    goal = Goal(id="goal-attempt", project_id="p", description="attempt lineage")
    sop = SopDefinition(
        id="sop-attempt",
        name="plan execute review",
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
                        handoff_to="execute",
                    ),
                    StepDefinition(
                        id="execute",
                        name="Execute",
                        role_id="dsh",
                        output_type=ArtifactType.IMPLEMENTATION,
                        depends_on=["plan"],
                        handoff_to="review",
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
    return goal, sop


def _resolvers():
    def resolve_assignment(task, step) -> SubagentAssignment:
        return SubagentAssignment(
            id=f"assign-{task.id}",
            task_id=task.id,
            role_id=task.role_id,
            agent_instance_id="agent",
            runtime_id=task.role_id,
        )

    def build_context(task, step) -> ContextPackage:
        return ContextPackage(
            id=f"ctx-{task.id}",
            task_id=task.id,
            goal_summary="attempt lineage",
            instructions=step.instructions or step.name,
        )

    return resolve_assignment, build_context


@pytest.fixture
def pass_engine(tmp_path):
    store = JsonWorkflowStore(tmp_path / "state.json", tmp_path / "events.jsonl")
    runner = PassRunner()
    engine = WorkflowEngine(
        store=store,
        runners={"claude": runner, "dsh": runner, "codex": runner},
        validator=AcceptAll(),
    )
    return engine, store


def test_attempt_model_defaults():
    attempt = Attempt(id="att1", task_id="t1")
    assert attempt.sequence == 1
    assert attempt.status is TaskStatus.PENDING
    assert attempt.schema_version == 2
    assert attempt.previous_attempt_id is None


def test_artifact_attempt_id_optional_legacy():
    legacy = Artifact(id="a1", task_id="t1", type=ArtifactType.DIFF)
    assert legacy.attempt_id is None
    current = Artifact(
        id="a2", task_id="t1", attempt_id="att1", type=ArtifactType.DIFF
    )
    assert current.attempt_id == "att1"


def test_review_reviewed_attempt_id_optional_legacy():
    legacy = Review(
        id="r1", task_id="t1", artifact_id="a1", reviewer_role_id="codex"
    )
    assert legacy.reviewed_attempt_id is None
    current = Review(
        id="r2",
        task_id="t1",
        artifact_id="a1",
        reviewer_role_id="codex",
        reviewed_attempt_id="att1",
    )
    assert current.reviewed_attempt_id == "att1"


def test_create_task_creates_attempt(pass_engine):
    engine, store = pass_engine
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    step_run = engine.ready_step_runs(run.id)[0]
    task = engine.create_task(step_run.id, role_id="claude")
    attempts = store.list_entities("attempts")
    assert len(attempts) == 1
    attempt = Attempt.model_validate(attempts[0])
    assert attempt.task_id == task.id
    assert attempt.sequence == 1
    events = [e.event_type for e in store.replay_events(run.id)]
    assert "attempt_created" in events


@pytest.mark.asyncio
async def test_full_loop_attempt_lineage(pass_engine):
    """Every task/artifact/review is bound to its own Attempt."""
    engine, store = pass_engine
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    orchestrator = AutoOrchestrator(engine)
    await orchestrator.run_until_gate(
        run.id,
        resolve_assignment=_resolvers()[0],
        build_context=_resolvers()[1],
    )

    tasks = store.list_entities("tasks")
    attempts = store.list_entities("attempts")
    artifacts = store.list_entities("artifacts")
    reviews = store.list_entities("reviews")

    assert len(tasks) == 3
    assert len(attempts) == 3  # one Attempt per new task execution
    assert len(artifacts) == 4  # PLAN + (DIFF, TEST_REPORT) + REVIEW_REPORT

    by_task = {a["task_id"]: a for a in attempts}
    for task_dict in tasks:
        attempt = Attempt.model_validate(by_task[task_dict["id"]])
        assert attempt.status is TaskStatus.ACCEPTED

    for art_dict in artifacts:
        assert art_dict["attempt_id"] is not None
        assert art_dict["attempt_id"] == by_task[art_dict["task_id"]]["id"]
        assert art_dict["schema_version"] == 2

    executor_artifacts = [
        a
        for a in artifacts
        if a["task_id"] in {t["id"] for t in tasks if t["role_id"] == "dsh"}
    ]
    assert {a["type"] for a in executor_artifacts} >= {
        ArtifactType.DIFF.value,
        ArtifactType.TEST_REPORT.value,
    }
    assert len({a["attempt_id"] for a in executor_artifacts}) == 1

    assert len(reviews) == 1
    review = Review.model_validate(reviews[0])
    executor = next(t for t in tasks if t["role_id"] == "dsh")
    assert review.reviewed_task_id == executor["id"]
    assert review.reviewed_attempt_id == by_task[executor["id"]]["id"]
    assert review.status.value == "approved"


@pytest.mark.asyncio
async def test_rework_links_attempts(tmp_path):
    """REWORK -> retry creates a fresh Attempt linked via previous_attempt_id."""
    store = JsonWorkflowStore(tmp_path / "state.json", tmp_path / "events.jsonl")
    rework_runner = ReworkRunner()
    engine = WorkflowEngine(
        store=store,
        runners={"claude": rework_runner, "dsh": rework_runner, "codex": rework_runner},
        validator=AcceptAll(),
    )
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    orchestrator = AutoOrchestrator(engine)
    await orchestrator.run_until_gate(
        run.id,
        resolve_assignment=_resolvers()[0],
        build_context=_resolvers()[1],
    )

    tasks = store.list_entities("tasks")
    attempts = store.list_entities("attempts")
    executor_tasks = [t for t in tasks if t["role_id"] == "dsh"]
    assert len(executor_tasks) == 2  # original + retry

    executor_attempts = [
        Attempt.model_validate(a)
        for a in attempts
        if a["task_id"] in {t["id"] for t in executor_tasks}
    ]
    assert len(executor_attempts) == 2

    retry_task = next(t for t in executor_tasks if t["retry_of"])
    retry_attempt = next(a for a in executor_attempts if a.task_id == retry_task["id"])
    original_task = next(t for t in executor_tasks if not t["retry_of"])
    original_attempt = next(
        a for a in executor_attempts if a.task_id == original_task["id"]
    )
    assert retry_attempt.previous_attempt_id == original_attempt.id

    reviews = store.list_entities("reviews")
    assert len(reviews) == 2
    final_review = Review.model_validate(reviews[-1])
    assert final_review.status.value == "approved"
    assert final_review.reviewer_output == "PASS"
    assert final_review.policy_decision == "APPROVED"
    # Phase 3 current-attempt binding: the final review must pin the RETRY
    # attempt's evidence, never the historical one.
    assert final_review.reviewed_attempt_id == retry_attempt.id
    assert set(final_review.evidence_ids) == {
        f"diff-{retry_task['id']}",
        f"test-{retry_task['id']}",
    }
    assert retry_attempt.previous_attempt_id == original_attempt.id


def test_legacy_artifact_review_materializes_attempt(tmp_path):
    """A NEW Review over legacy evidence must still carry reviewed_attempt_id
    (synthetic attempt #1 materialized on the legacy read path)."""
    store = JsonWorkflowStore(tmp_path / "state.json", tmp_path / "events.jsonl")
    engine = WorkflowEngine(store=store, runner=PassRunner(), validator=AcceptAll())

    legacy_task = Task(
        id="legacy-executor", step_run_id="execute-step-run", role_id="dsh"
    )
    review_task = Task(
        id="reviewer", step_run_id="review-step-run", role_id="codex"
    )
    store.save_entity("tasks", legacy_task)
    store.save_entity("tasks", review_task)
    store.save_entity(
        "step_runs",
        StepRun(
            id="review-step-run",
            sop_run_id="run",
            step_id="review",
            status=StepStatus.WAITING_REVIEW,
        ),
    )
    legacy_artifact = Artifact(
        id="legacy-output",
        task_id=legacy_task.id,
        type=ArtifactType.IMPLEMENTATION,
    )
    store.save_entity("artifacts", legacy_artifact)
    report = Artifact(
        id="report", task_id=review_task.id, type=ArtifactType.REVIEW_REPORT
    )
    store.save_entity("artifacts", report)

    review = engine.ensure_review_for_report(
        task=review_task,
        artifact=report,
        context=ContextPackage(
            id="ctx",
            task_id=review_task.id,
            goal_summary="",
            instructions="",
            artifact_ids=[legacy_artifact.id],
        ),
    )
    assert review.reviewed_task_id == legacy_task.id
    assert review.reviewed_attempt_id is not None
    attempt = Attempt.model_validate(
        store.get_entity("attempts", review.reviewed_attempt_id)
    )
    assert attempt.task_id == legacy_task.id
    assert attempt.sequence == 1


def test_retry_legacy_task_links_attempt(tmp_path):
    """Retrying a legacy (attempt-less) task materializes the parent attempt
    and links the new Attempt via previous_attempt_id."""
    store = JsonWorkflowStore(tmp_path / "state.json", tmp_path / "events.jsonl")
    engine = WorkflowEngine(store=store, runner=PassRunner(), validator=AcceptAll())
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    step_run = engine.ready_step_runs(run.id)[0]

    legacy_task = Task(
        id="legacy-t",
        step_run_id=step_run.id,
        role_id="claude",
        status=TaskStatus.REJECTED,
    )
    store.save_entity("tasks", legacy_task)
    step_run.task_id = legacy_task.id
    store.save_entity("step_runs", step_run)

    retry = engine.retry_task(legacy_task.id)
    parent_attempt = engine._current_attempt_for_task(legacy_task.id)
    assert parent_attempt is not None
    retry_attempt = engine._current_attempt_for_task(retry.id)
    assert retry_attempt is not None
    assert retry_attempt.previous_attempt_id == parent_attempt.id
