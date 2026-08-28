"""End-to-end test for minimal 2-step SOP execution."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from workbench.backend.agents.fake_runner import FakeSubagentRunner
from workbench.backend.artifacts.store import FileArtifactStore
from workbench.backend.domain.models import (
    AcceptanceCriteria,
    ArtifactType,
    ExecutionMode,
    Goal,
    GoalStatus,
    HandoffStatus,
    SopDefinition,
    SopRunStatus,
    StageDefinition,
    StepDefinition,
    StepStatus,
    SubagentAssignment,
    TaskStatus,
)
from workbench.backend.persistence.store import JsonWorkflowStore
from workbench.backend.validation.always_accept import AlwaysAcceptValidator
from workbench.backend.workflow.context_builder import ContextPackageBuilder
from workbench.backend.workflow.engine import WorkflowEngine
from workbench.backend.workflow.orchestrator import AutoOrchestrator


@pytest.fixture
def workspace(tmp_path: Path):
    """Create test workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.fixture
def stores(tmp_path: Path):
    """Create persistence stores."""
    state_path = tmp_path / "state.json"
    event_path = tmp_path / "events.jsonl"
    artifact_root = tmp_path / "artifacts"
    return {
        "workflow_store": JsonWorkflowStore(state_path, event_path),
        "artifact_store": FileArtifactStore(artifact_root),
    }


@pytest.fixture
def engine(stores):
    """Create workflow engine with fake runner."""
    runner = FakeSubagentRunner()
    validator = AlwaysAcceptValidator()
    engine = WorkflowEngine(
        store=stores["workflow_store"],
        runner=runner,
        validator=validator,
        artifact_store=stores["artifact_store"],
    )
    return engine


def create_two_step_sop() -> tuple[Goal, SopDefinition]:
    """Create a simple 2-step SOP: research → summarize."""
    goal = Goal(
        id=str(uuid.uuid4()),
        project_id="test-project",
        description="Research a topic and create a summary",
        acceptance_criteria=["Research completed", "Summary created"],
        status=GoalStatus.RUNNING,
        created_at=datetime.now(UTC),
    )

    sop = SopDefinition(
        id=str(uuid.uuid4()),
        name="Research and Summarize",
        version="1.0",
        goal_template="Research topic: {topic}",
        stages=[
            StageDefinition(
                id="stage-1",
                name="Execution",
                description="Research and summarize",
                steps=[
                    StepDefinition(
                        id="step-research",
                        name="Research",
                        role_id="researcher",
                        instructions="Research the given topic thoroughly",
                        acceptance_criteria=[
                            AcceptanceCriteria(
                                id="ac-research-1",
                                description="Research findings documented",
                                required=True,
                            )
                        ],
                        execution_mode=ExecutionMode.SEQUENTIAL,
                        depends_on=[],
                        requires_review=False,
                        handoff_to="step-summarize",
                    ),
                    StepDefinition(
                        id="step-summarize",
                        name="Summarize",
                        role_id="summarizer",
                        instructions="Create a summary from research findings",
                        acceptance_criteria=[
                            AcceptanceCriteria(
                                id="ac-summarize-1",
                                description="Summary completed",
                                required=True,
                            )
                        ],
                        execution_mode=ExecutionMode.SEQUENTIAL,
                        depends_on=["step-research"],
                        requires_review=False,
                        handoff_to=None,
                    ),
                ],
            )
        ],
    )
    return goal, sop


@pytest.mark.asyncio
async def test_two_step_sop_completes_with_fake_runner(engine: WorkflowEngine, stores):
    """Test complete 2-step SOP execution using FakeSubagentRunner."""
    goal, sop = create_two_step_sop()

    # Start SOP run
    sop_run = engine.start_sop_run(goal=goal, sop=sop)
    assert sop_run.status is SopRunStatus.RUNNING
    assert sop_run.goal_id == goal.id

    # Verify SOP persisted
    loaded_run = stores["workflow_store"].get_entity("sop_runs", sop_run.id)
    assert loaded_run["id"] == sop_run.id

    # Verify events emitted
    events = stores["workflow_store"].replay_events(sop_run.id)
    assert len(events) > 0
    assert events[0].event_type == "sop_started"

    # Execute Step 1: Research
    context_builder = ContextPackageBuilder(
        store=stores["workflow_store"],
        artifact_store=stores["artifact_store"],
    )

    ready_steps = engine.ready_step_runs(sop_run.id)
    assert len(ready_steps) == 1
    step_run_1 = ready_steps[0]
    assert step_run_1.step_id == "step-research"
    assert step_run_1.status is StepStatus.READY

    step_def_1 = engine.step_definition(sop_run.id, "step-research")
    task_1 = engine.create_task(step_run_1.id, role_id="researcher")
    assert task_1.status is TaskStatus.PENDING

    assignment_1 = SubagentAssignment(
        id=str(uuid.uuid4()),
        task_id=task_1.id,
        role_id="researcher",
        agent_instance_id="fake-agent-1",
        runtime_id="fake-runtime",
    )
    context_1 = context_builder.build_for_task(
        task=task_1, step=step_def_1, goal=goal, sop=sop
    )
    assert context_1.task_id == task_1.id
    assert len(context_1.artifact_ids) == 0  # No upstream artifacts

    result_1 = await engine.execute_task(
        task_1.id, assignment=assignment_1, context=context_1
    )
    assert result_1.task.status is TaskStatus.ACCEPTED
    assert result_1.artifact.accepted is True
    assert result_1.validation.status.value == "accepted"
    assert result_1.handoff is not None
    assert result_1.handoff.to_step_id == "step-summarize"
    assert result_1.handoff.status is HandoffStatus.READY

    # Verify artifact persisted
    artifact_1 = stores["artifact_store"].get(result_1.artifact)
    assert b"Fake artifact output" in artifact_1
    assert b"Task: Research" in artifact_1

    # Accept handoff
    step_run_2 = engine.accept_handoff(result_1.handoff.id)
    assert step_run_2.step_id == "step-summarize"
    assert step_run_2.status is StepStatus.READY

    # Verify handoff updated
    handoff = stores["workflow_store"].get_entity("handoffs", result_1.handoff.id)
    assert handoff["status"] == HandoffStatus.ACCEPTED.value

    # Execute Step 2: Summarize
    ready_steps_2 = engine.ready_step_runs(sop_run.id)
    assert len(ready_steps_2) == 1
    assert ready_steps_2[0].step_id == "step-summarize"

    step_def_2 = engine.step_definition(sop_run.id, "step-summarize")
    task_2 = engine.create_task(step_run_2.id, role_id="summarizer")

    assignment_2 = SubagentAssignment(
        id=str(uuid.uuid4()),
        task_id=task_2.id,
        role_id="summarizer",
        agent_instance_id="fake-agent-2",
        runtime_id="fake-runtime",
    )
    context_2 = context_builder.build_for_task(
        task=task_2, step=step_def_2, goal=goal, sop=sop
    )
    assert context_2.task_id == task_2.id
    assert len(context_2.artifact_ids) == 1  # Research artifact
    assert context_2.artifact_ids[0] == result_1.artifact.id

    result_2 = await engine.execute_task(
        task_2.id, assignment=assignment_2, context=context_2
    )
    assert result_2.task.status is TaskStatus.ACCEPTED
    assert result_2.artifact.accepted is True
    assert result_2.handoff is None  # No more steps

    # Verify SOP completed
    final_run = engine.store.get_entity("sop_runs", sop_run.id)
    assert final_run["status"] == SopRunStatus.COMPLETED.value

    # Verify all steps completed
    all_step_runs = [
        item
        for item in stores["workflow_store"].list_entities("step_runs")
        if item["sop_run_id"] == sop_run.id
    ]
    assert len(all_step_runs) == 2
    assert all(item["status"] == StepStatus.COMPLETED.value for item in all_step_runs)

    # Verify event stream
    final_events = stores["workflow_store"].replay_events(sop_run.id)
    event_types = [event.event_type for event in final_events]
    assert "sop_started" in event_types
    assert "step_started" in event_types
    assert "task_created" in event_types
    assert "handoff_created" in event_types
    assert "handoff_accepted" in event_types
    assert "step_completed" in event_types
    assert "sop_completed" in event_types

    # Verify sop_completed is the last event
    assert final_events[-1].event_type == "sop_completed"


@pytest.mark.asyncio
async def test_auto_orchestrator_runs_until_completion(engine: WorkflowEngine, stores):
    """Test AutoOrchestrator automatically advances through all steps."""
    goal, sop = create_two_step_sop()
    sop_run = engine.start_sop_run(goal=goal, sop=sop)

    orchestrator = AutoOrchestrator(engine)
    context_builder = ContextPackageBuilder(
        store=stores["workflow_store"],
        artifact_store=stores["artifact_store"],
    )

    def resolve_assignment(task, step):
        return SubagentAssignment(
            id=str(uuid.uuid4()),
            task_id=task.id,
            role_id=step.role_id,
            agent_instance_id=f"fake-agent-{step.role_id}",
            runtime_id="fake-runtime",
        )

    def build_context(task, step):
        return context_builder.build_for_task(
            task=task, step=step, goal=goal, sop=sop
        )

    results = await orchestrator.run_until_gate(
        sop_run.id,
        resolve_assignment=resolve_assignment,
        build_context=build_context,
    )

    # Verify both steps executed
    assert len(results) == 2
    assert results[0].task.title == "Research"
    assert results[1].task.title == "Summarize"

    # Verify SOP completed
    final_run = engine.store.get_entity("sop_runs", sop_run.id)
    assert final_run["status"] == SopRunStatus.COMPLETED.value

    # Verify persistence
    all_tasks = stores["workflow_store"].list_entities("tasks")
    assert len(all_tasks) == 2

    all_artifacts = stores["workflow_store"].list_entities("artifacts")
    assert len(all_artifacts) == 2
    assert all(item["accepted"] is True for item in all_artifacts)


@pytest.mark.asyncio
async def test_context_package_includes_upstream_artifacts(engine: WorkflowEngine, stores):
    """Test that step 2 receives step 1's artifact in context."""
    goal, sop = create_two_step_sop()
    sop_run = engine.start_sop_run(goal=goal, sop=sop)

    orchestrator = AutoOrchestrator(engine)
    context_builder = ContextPackageBuilder(
        store=stores["workflow_store"],
        artifact_store=stores["artifact_store"],
    )

    captured_contexts = []

    def resolve_assignment(task, step):
        return SubagentAssignment(
            id=str(uuid.uuid4()),
            task_id=task.id,
            role_id=step.role_id,
            agent_instance_id=f"agent-{step.role_id}",
            runtime_id="fake",
        )

    def build_context(task, step):
        context = context_builder.build_for_task(
            task=task, step=step, goal=goal, sop=sop
        )
        captured_contexts.append((step.id, context))
        return context

    await orchestrator.run_until_gate(
        sop_run.id,
        resolve_assignment=resolve_assignment,
        build_context=build_context,
    )

    # Verify step 1 has no upstream artifacts
    step_1_context = next(ctx for step_id, ctx in captured_contexts if step_id == "step-research")
    assert len(step_1_context.artifact_ids) == 0

    # Verify step 2 has step 1's artifact
    step_2_context = next(ctx for step_id, ctx in captured_contexts if step_id == "step-summarize")
    assert len(step_2_context.artifact_ids) == 1

    # Verify the artifact is from step 1
    artifact_id = step_2_context.artifact_ids[0]
    artifact = stores["workflow_store"].get_entity("artifacts", artifact_id)
    assert artifact["accepted"] is True
    assert "step-research" in artifact["producer_step_run_id"]
