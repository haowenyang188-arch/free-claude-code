"""M7: Test sop_run_id isolation in context builder."""

from __future__ import annotations

import pytest

from workbench.backend.domain.models import (
    Artifact,
    ArtifactType,
    Goal,
    Handoff,
    SopDefinition,
    StageDefinition,
    StepDefinition,
    StepRun,
    Task,
    TaskStatus,
)
from workbench.backend.persistence.store import JsonWorkflowStore
from workbench.backend.workflow.context_builder import ContextPackageBuilder


@pytest.fixture
def store(tmp_path):
    """Create a fresh workflow store."""
    return JsonWorkflowStore(
        state_path=str(tmp_path / "state"),
        event_path=str(tmp_path / "events"),
    )


@pytest.fixture
def builder(store):
    """Create context builder with store."""
    return ContextPackageBuilder(store=store)


def test_context_builder_filters_artifacts_by_sop_run_id(store, builder):
    """M7: Context builder should only collect artifacts from the same SOP run."""
    # Create two SOP runs
    sop_def = SopDefinition(
        id="sop-1",
        name="Test SOP",
        stages=[
            StageDefinition(
                id="stage-execute",
                name="EXECUTE",
                steps=[
                    StepDefinition(
                        id="step-1",
                        name="Step 1",
                        role_id="claude",
                        depends_on=[],
                    ),
                    StepDefinition(
                        id="step-2",
                        name="Step 2",
                        role_id="codex",
                        depends_on=["step-1"],
                    ),
                ],
            )
        ],
    )
    store.save_entity("sop_definitions", sop_def)

    # SOP run A
    step_run_a1 = StepRun(
        id="run-a:step-1",
        sop_run_id="run-a",
        step_id="step-1",
        status="completed",
    )
    step_run_a2 = StepRun(
        id="run-a:step-2",
        sop_run_id="run-a",
        step_id="step-2",
        status="pending",
    )
    store.save_entity("step_runs", step_run_a1)
    store.save_entity("step_runs", step_run_a2)

    # SOP run B
    step_run_b1 = StepRun(
        id="run-b:step-1",
        sop_run_id="run-b",
        step_id="step-1",
        status="completed",
    )
    step_run_b2 = StepRun(
        id="run-b:step-2",
        sop_run_id="run-b",
        step_id="step-2",
        status="pending",
    )
    store.save_entity("step_runs", step_run_b1)
    store.save_entity("step_runs", step_run_b2)

    # Create artifacts from step-1 in both runs
    artifact_a = Artifact(
        id="artifact-a",
        task_id="task-a1",
        producer_step_run_id="run-a:step-1",
        type=ArtifactType.TEXT,
        content="code from run A",
        accepted=True,
    )
    artifact_b = Artifact(
        id="artifact-b",
        task_id="task-b1",
        producer_step_run_id="run-b:step-1",
        type=ArtifactType.TEXT,
        content="code from run B",
        accepted=True,
    )
    store.save_entity("artifacts", artifact_a)
    store.save_entity("artifacts", artifact_b)

    # Create task for step-2 in run A
    task_a2 = Task(
        id="task-a2",
        step_run_id="run-a:step-2",
        role_id="codex",
        title="Task A2",
        description="Execute step 2 in run A",
        status=TaskStatus.PENDING,
    )
    store.save_entity("tasks", task_a2)

    # Create a minimal goal
    goal = Goal(
        id="goal-1",
        project_id="project-1",
        description="Test goal",
        status="running",
    )

    # Build context for task A2
    step_2_def = sop_def.stages[0].steps[1]
    context = builder.build_for_task(
        task=task_a2, step=step_2_def, goal=goal, sop=sop_def
    )

    # M7: Should only include artifact from run A, not run B
    assert "artifact-a" in context.artifact_ids
    assert "artifact-b" not in context.artifact_ids


def test_context_builder_filters_handoffs_by_sop_run_id(store, builder):
    """M7: Context builder should only collect handoffs from the same SOP run."""
    # Create SOP with two stages
    sop_def = SopDefinition(
        id="sop-1",
        name="Test SOP",
        stages=[
            StageDefinition(
                id="stage-plan",
                name="PLAN",
                steps=[
                    StepDefinition(
                        id="step-plan",
                        name="Plan",
                        role_id="claude",
                        depends_on=[],
                    )
                ],
            ),
            StageDefinition(
                id="stage-execute",
                name="EXECUTE",
                steps=[
                    StepDefinition(
                        id="step-exec",
                        name="Execute",
                        role_id="codex",
                        depends_on=["step-plan"],
                    )
                ],
            ),
        ],
    )
    store.save_entity("sop_definitions", sop_def)

    # SOP run A
    step_run_a_plan = StepRun(
        id="run-a:step-plan",
        sop_run_id="run-a",
        step_id="step-plan",
        status="completed",
    )
    step_run_a_exec = StepRun(
        id="run-a:step-exec",
        sop_run_id="run-a",
        step_id="step-exec",
        status="pending",
    )
    store.save_entity("step_runs", step_run_a_plan)
    store.save_entity("step_runs", step_run_a_exec)

    # SOP run B
    step_run_b_plan = StepRun(
        id="run-b:step-plan",
        sop_run_id="run-b",
        step_id="step-plan",
        status="completed",
    )
    store.save_entity("step_runs", step_run_b_plan)

    # Create tasks
    task_a_plan = Task(
        id="task-a-plan",
        step_run_id="run-a:step-plan",
        role_id="claude",
        title="Plan A",
        status=TaskStatus.ACCEPTED,
    )
    task_b_plan = Task(
        id="task-b-plan",
        step_run_id="run-b:step-plan",
        role_id="claude",
        title="Plan B",
        status=TaskStatus.ACCEPTED,
    )
    task_a_exec = Task(
        id="task-a-exec",
        step_run_id="run-a:step-exec",
        role_id="codex",
        title="Execute A",
        status=TaskStatus.PENDING,
    )
    store.save_entity("tasks", task_a_plan)
    store.save_entity("tasks", task_b_plan)
    store.save_entity("tasks", task_a_exec)

    # Create handoffs from both runs
    handoff_a = Handoff(
        id="handoff-a",
        from_task_id="task-a-plan",
        to_step_id="step-exec",
        artifact_ids=["artifact-a"],
        status="accepted",
    )
    handoff_b = Handoff(
        id="handoff-b",
        from_task_id="task-b-plan",
        to_step_id="step-exec",
        artifact_ids=["artifact-b"],
        status="accepted",
    )
    store.save_entity("handoffs", handoff_a)
    store.save_entity("handoffs", handoff_b)

    # Create a minimal goal
    goal = Goal(
        id="goal-1",
        project_id="project-1",
        description="Test goal",
        status="running",
    )

    # Build context for execute task in run A
    step_exec_def = sop_def.stages[1].steps[0]
    context = builder.build_for_task(
        task=task_a_exec, step=step_exec_def, goal=goal, sop=sop_def
    )

    # M7: Should only include artifact from handoff A (same SOP run)
    assert "artifact-a" in context.artifact_ids
    assert "artifact-b" not in context.artifact_ids
