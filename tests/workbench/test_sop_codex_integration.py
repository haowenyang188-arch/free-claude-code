"""Real Codex integration test for 2-step SOP execution."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from workbench.backend.agents.codex_adapter import CodexAdapter
from workbench.backend.agents.codex_runner import CodexSubagentRunner
from workbench.backend.artifacts.store import FileArtifactStore
from workbench.backend.domain.models import (
    AcceptanceCriteria,
    ExecutionMode,
    Goal,
    GoalStatus,
    SopDefinition,
    SopRunStatus,
    StageDefinition,
    StepDefinition,
    StepStatus,
    SubagentAssignment,
)
from workbench.backend.persistence.store import JsonWorkflowStore
from workbench.backend.runtime.approval import ApprovalManager
from workbench.backend.validation.always_accept import AlwaysAcceptValidator
from workbench.backend.workflow.context_builder import ContextPackageBuilder
from workbench.backend.workflow.engine import WorkflowEngine
from workbench.backend.workflow.orchestrator import AutoOrchestrator


@pytest.fixture
def workspace(tmp_path: Path):
    """Create test workspace with a sample file."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("# Test Project\n\nSample content for testing.\n")
    return workspace


@pytest.fixture
def stores(tmp_path: Path):
    """Create persistence stores."""
    state_path = tmp_path / "sop_state.json"
    event_path = tmp_path / "sop_events.jsonl"
    artifact_root = tmp_path / "sop_artifacts"
    return {
        "workflow_store": JsonWorkflowStore(state_path, event_path),
        "artifact_store": FileArtifactStore(artifact_root),
    }


async def create_codex_engine(workspace: Path, stores):
    """Create workflow engine with real Codex runner."""
    approval_manager = ApprovalManager()
    adapter = CodexAdapter(
        agent_id="codex-sop-runner",
        use_app_server=True,
        approval_manager=approval_manager,
    )

    # Check availability
    available = await adapter.check_availability()
    if not available:
        pytest.skip("Codex not available or not in safe mode")

    runner = CodexSubagentRunner(
        codex_adapter=adapter,
        workspace_path=workspace,
    )
    validator = AlwaysAcceptValidator()
    engine = WorkflowEngine(
        store=stores["workflow_store"],
        runner=runner,
        validator=validator,
        artifact_store=stores["artifact_store"],
    )

    yield engine

    # Cleanup
    await adapter.cleanup()


def create_simple_file_sop() -> tuple[Goal, SopDefinition]:
    """Create a simple 2-step SOP: read file → count words."""
    goal = Goal(
        id=str(uuid.uuid4()),
        project_id="test-project",
        description="Read README.md and count the number of words",
        acceptance_criteria=["File read", "Word count reported"],
        status=GoalStatus.RUNNING,
        created_at=datetime.now(UTC),
    )

    sop = SopDefinition(
        id=str(uuid.uuid4()),
        name="Read and Count",
        version="1.0",
        goal_template="Read file and count words",
        stages=[
            StageDefinition(
                id="stage-1",
                name="Execution",
                description="Read and count",
                steps=[
                    StepDefinition(
                        id="step-read",
                        name="Read File",
                        role_id="reader",
                        instructions="Read the README.md file and output its content",
                        acceptance_criteria=[
                            AcceptanceCriteria(
                                id="ac-read-1",
                                description="File content extracted",
                                required=True,
                            )
                        ],
                        execution_mode=ExecutionMode.SEQUENTIAL,
                        depends_on=[],
                        requires_review=False,
                        handoff_to="step-count",
                    ),
                    StepDefinition(
                        id="step-count",
                        name="Count Words",
                        role_id="counter",
                        instructions="Count the number of words in the file content provided by the previous step",
                        acceptance_criteria=[
                            AcceptanceCriteria(
                                id="ac-count-1",
                                description="Word count reported",
                                required=True,
                            )
                        ],
                        execution_mode=ExecutionMode.SEQUENTIAL,
                        depends_on=["step-read"],
                        requires_review=False,
                        handoff_to=None,
                    ),
                ],
            )
        ],
    )
    return goal, sop


@pytest.mark.asyncio
@pytest.mark.integration
async def test_codex_two_step_sop_execution(
    stores,
    workspace: Path,
):
    """Test real Codex executing a 2-step SOP."""
    # Create engine with Codex
    async for engine_with_codex in create_codex_engine(workspace, stores):
        break

    goal, sop = create_simple_file_sop()

    # Start SOP run
    sop_run = engine_with_codex.start_sop_run(goal=goal, sop=sop)
    assert sop_run.status is SopRunStatus.RUNNING

    # Use orchestrator for automatic execution
    orchestrator = AutoOrchestrator(engine_with_codex)
    context_builder = ContextPackageBuilder(
        store=stores["workflow_store"],
        artifact_store=stores["artifact_store"],
    )

    def resolve_assignment(task, step):
        return SubagentAssignment(
            id=str(uuid.uuid4()),
            task_id=task.id,
            role_id=step.role_id,
            agent_instance_id="codex-sop-runner",
            runtime_id="codex",
        )

    def build_context(task, step):
        return context_builder.build_for_task(
            task=task, step=step, goal=goal, sop=sop
        )

    # Execute all steps
    results = await orchestrator.run_until_gate(
        sop_run.id,
        resolve_assignment=resolve_assignment,
        build_context=build_context,
    )

    # Verify both steps executed
    assert len(results) == 2
    assert results[0].task.title == "Read File"
    assert results[1].task.title == "Count Words"

    # Verify artifacts created
    assert results[0].artifact.content is not None
    assert results[1].artifact.content is not None

    # Verify SOP completed
    final_run = engine_with_codex.store.get_entity("sop_runs", sop_run.id)
    assert final_run["status"] == SopRunStatus.COMPLETED.value

    # Verify persistence
    all_tasks = stores["workflow_store"].list_entities("tasks")
    assert len(all_tasks) == 2

    all_artifacts = stores["workflow_store"].list_entities("artifacts")
    assert len(all_artifacts) == 2
    assert all(item["accepted"] is True for item in all_artifacts)

    # Verify artifacts persisted to FileArtifactStore
    artifact_1 = stores["artifact_store"].get(results[0].artifact)
    artifact_2 = stores["artifact_store"].get(results[1].artifact)
    assert len(artifact_1) > 0
    assert len(artifact_2) > 0

    # Verify event stream
    events = stores["workflow_store"].replay_events(sop_run.id)
    event_types = [event.event_type for event in events]
    assert "sop_started" in event_types
    assert "step_completed" in event_types
    assert event_types.count("step_completed") == 2

    print(f"\n✅ SOP completed successfully!")
    print(f"📝 Step 1 artifact: {len(artifact_1)} bytes")
    print(f"📝 Step 2 artifact: {len(artifact_2)} bytes")
    print(f"📊 Total events: {len(events)}")
