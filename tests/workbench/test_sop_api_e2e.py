"""HTTP E2E test for SOP Control Plane API."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from workbench.backend.domain.models import (
    AcceptanceCriteria,
    ExecutionMode,
    Goal,
    SopDefinition,
    StageDefinition,
    StepDefinition,
)
from workbench.backend.main import app
from workbench.backend.runtime.workspace import WorkspacePolicy
from workbench.backend.agents.fake_runner import FakeSubagentRunner
from workbench.backend.validation.always_accept import AlwaysAcceptValidator
from workbench.backend.artifacts.store import FileArtifactStore
from workbench.backend.persistence.store import JsonWorkflowStore
from workbench.backend.workflow.engine import WorkflowEngine
from workbench.backend.workflow.orchestrator import AutoOrchestrator
from workbench.backend.workflow.context_builder import ContextPackageBuilder


@pytest.fixture
def isolated_sop_components(tmp_path):
    """Create isolated SOP workflow components for each test.

    This fixture replaces the global service's SOP components with
    isolated instances to prevent test interference.
    """
    from workbench.backend.main import service

    # Save original components
    original_store = service.workflow_store
    original_artifact_store = service.artifact_store
    original_engine = service.workflow_engine
    original_context_builder = service.context_builder
    original_orchestrator = service.orchestrator
    original_definitions = service.sop_definitions

    # Create isolated workspace
    sop_workspace = tmp_path / "sop"
    sop_workspace.mkdir(parents=True, exist_ok=True)

    # Replace with isolated components
    service.workflow_store = JsonWorkflowStore(
        state_path=sop_workspace / "workflow_state.json",
        event_path=sop_workspace / "workflow_events.jsonl",
    )
    service.artifact_store = FileArtifactStore(root=sop_workspace / "artifacts")

    runner = FakeSubagentRunner()
    validator = AlwaysAcceptValidator()

    service.workflow_engine = WorkflowEngine(
        store=service.workflow_store,
        runner=runner,
        validator=validator,
        artifact_store=service.artifact_store,
    )
    service.context_builder = ContextPackageBuilder(
        store=service.workflow_store,
        artifact_store=service.artifact_store,
    )
    service.orchestrator = AutoOrchestrator(service.workflow_engine)
    service.sop_definitions = {}

    yield service

    # Restore original components
    service.workflow_store = original_store
    service.artifact_store = original_artifact_store
    service.workflow_engine = original_engine
    service.context_builder = original_context_builder
    service.orchestrator = original_orchestrator
    service.sop_definitions = original_definitions


@pytest.fixture
def sop_definition():
    """Create a simple 2-step SOP definition."""
    # Use unique ID for each test to avoid conflicts
    import time
    unique_id = f"test-sop-{int(time.time() * 1000000)}"
    return SopDefinition(
        id=unique_id,
        name="Test SOP",
        version="1.0",
        goal_template="Test goal",
        stages=[
            StageDefinition(
                id="stage-1",
                name="Test Stage",
                description="Test stage",
                steps=[
                    StepDefinition(
                        id="step-a",
                        name="Step A",
                        role_id="tester",
                        instructions="Do step A",
                        acceptance_criteria=[
                            AcceptanceCriteria(
                                id="ac-a-1",
                                description="A completed",
                                required=True,
                            )
                        ],
                        execution_mode=ExecutionMode.SEQUENTIAL,
                        depends_on=[],
                        requires_review=False,
                        handoff_to="step-b",
                    ),
                    StepDefinition(
                        id="step-b",
                        name="Step B",
                        role_id="tester",
                        instructions="Do step B",
                        acceptance_criteria=[
                            AcceptanceCriteria(
                                id="ac-b-1",
                                description="B completed",
                                required=True,
                            )
                        ],
                        execution_mode=ExecutionMode.SEQUENTIAL,
                        depends_on=["step-a"],
                        requires_review=False,
                        handoff_to=None,
                    ),
                ],
            )
        ],
    )


@pytest.mark.asyncio
async def test_sop_api_start_and_query(sop_definition: SopDefinition, isolated_sop_components):
    """Test starting a SOP run via API and querying its state."""
    # Register SOP definition
    isolated_sop_components.sop_definitions[sop_definition.id] = sop_definition

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Start SOP run
        response = await client.post(
            "/api/sop-runs",
            json={
                "goal_description": "Test goal via API",
                "sop_definition_id": sop_definition.id,
                "project_id": "test-project",
                "acceptance_criteria": ["API test passes"],
                "constraints": [],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "sop_run_id" in data
        assert "goal_id" in data
        assert data["status"] == "running"

        sop_run_id = data["sop_run_id"]

        # Get SOP run status
        response = await client.get(f"/api/sop-runs/{sop_run_id}")
        assert response.status_code == 200
        run_data = response.json()
        assert run_data["id"] == sop_run_id
        assert run_data["status"] == "running"
        assert run_data["step_count"] == 2
        assert run_data["steps_ready"] == 1  # step-a should be ready

        # Get steps
        response = await client.get(f"/api/sop-runs/{sop_run_id}/steps")
        assert response.status_code == 200
        steps = response.json()
        assert len(steps) == 2
        step_a = next(s for s in steps if s["step_id"] == "step-a")
        step_b = next(s for s in steps if s["step_id"] == "step-b")
        assert step_a["status"] == "ready"
        assert step_b["status"] == "pending"

        # Get tasks (should be empty initially)
        response = await client.get(f"/api/sop-runs/{sop_run_id}/tasks")
        assert response.status_code == 200
        tasks = response.json()
        assert len(tasks) == 0

        # Get artifacts (should be empty initially)
        response = await client.get(f"/api/sop-runs/{sop_run_id}/artifacts")
        assert response.status_code == 200
        artifacts = response.json()
        assert len(artifacts) == 0

        # Get handoffs (should be empty initially)
        response = await client.get(f"/api/sop-runs/{sop_run_id}/handoffs")
        assert response.status_code == 200
        handoffs = response.json()
        assert len(handoffs) == 0

        # Get events
        response = await client.get(f"/api/sop-runs/{sop_run_id}/events")
        assert response.status_code == 200
        events = response.json()
        assert len(events) > 0
        assert events[0]["event_type"] == "sop_started"


@pytest.mark.asyncio
async def test_sop_api_full_execution(sop_definition: SopDefinition, isolated_sop_components):
    """Test full SOP execution via Orchestrator and verify API queries."""
    # Register SOP definition
    isolated_sop_components.sop_definitions[sop_definition.id] = sop_definition

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Start SOP run
        response = await client.post(
            "/api/sop-runs",
            json={
                "goal_description": "Full execution test",
                "sop_definition_id": sop_definition.id,
                "project_id": "test-project",
            },
        )
        assert response.status_code == 200
        sop_run_id = response.json()["sop_run_id"]

        # Execute SOP through Orchestrator (not via API - Orchestrator controls flow)
        from workbench.backend.domain.models import Goal, GoalStatus, SubagentAssignment

        # Get the actual goal and sop_run from store
        sop_run_data = isolated_sop_components.workflow_store.get_entity("sop_runs", sop_run_id)
        goal = isolated_sop_components.workflow_store.get_entity("goals", sop_run_data["goal_id"])
        goal_obj = Goal.model_validate(goal)

        def resolve_assignment(task, step):
            return SubagentAssignment(
                id=str(uuid.uuid4()),
                task_id=task.id,
                role_id=step.role_id,
                agent_instance_id="fake-agent",
                runtime_id="fake",
            )

        def build_context(task, step):
            return isolated_sop_components.context_builder.build_for_task(
                task=task, step=step, goal=goal_obj, sop=sop_definition
            )

        # Run orchestrator
        results = await isolated_sop_components.orchestrator.run_until_gate(
            sop_run_id,
            resolve_assignment=resolve_assignment,
            build_context=build_context,
        )

        assert len(results) == 2  # Both steps executed

        # Query final state via API
        response = await client.get(f"/api/sop-runs/{sop_run_id}")
        assert response.status_code == 200
        run_data = response.json()
        assert run_data["status"] == "completed"
        assert run_data["steps_completed"] == 2

        # Get tasks
        response = await client.get(f"/api/sop-runs/{sop_run_id}/tasks")
        assert response.status_code == 200
        tasks = response.json()
        assert len(tasks) == 2
        assert all(t["status"] == "accepted" for t in tasks)

        # Get artifacts
        response = await client.get(f"/api/sop-runs/{sop_run_id}/artifacts")
        assert response.status_code == 200
        artifacts = response.json()
        assert len(artifacts) == 2
        assert all(a["accepted"] is True for a in artifacts)
        assert all(a["sha256"] is not None for a in artifacts)

        # Verify Step B context included Step A artifact
        task_b = next(t for t in tasks if "Step B" in t["title"])
        assert len(task_b["input_artifact_ids"]) >= 1  # At least one artifact from Step A

        artifact_a_id = task_b["input_artifact_ids"][0]
        artifact_a = next(a for a in artifacts if a["id"] == artifact_a_id)
        assert artifact_a["accepted"] is True

        # Get handoffs
        response = await client.get(f"/api/sop-runs/{sop_run_id}/handoffs")
        assert response.status_code == 200
        handoffs = response.json()
        assert len(handoffs) == 1
        assert handoffs[0]["status"] == "accepted"
        assert handoffs[0]["to_step_id"] == "step-b"

        # Get events and verify sop_completed is last
        response = await client.get(f"/api/sop-runs/{sop_run_id}/events")
        assert response.status_code == 200
        events = response.json()
        assert len(events) > 0
        assert events[0]["event_type"] == "sop_started"
        assert events[-1]["event_type"] == "sop_completed"

        # Verify event types
        event_types = [e["event_type"] for e in events]
        assert "step_started" in event_types
        assert "task_created" in event_types
        assert "artifact_created" in event_types
        assert "handoff_created" in event_types
        assert "handoff_accepted" in event_types
        assert "step_completed" in event_types


@pytest.mark.asyncio
async def test_sop_api_404_errors(isolated_sop_components):
    """Test API error handling."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Non-existent SOP definition
        response = await client.post(
            "/api/sop-runs",
            json={
                "goal_description": "Test",
                "sop_definition_id": "non-existent",
            },
        )
        assert response.status_code == 404

        # Non-existent SOP run
        response = await client.get("/api/sop-runs/non-existent-run")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_sop_events_pagination(isolated_sop_components):
    """Test event pagination with after parameter."""
    isolated_sop_components.sop_definitions["test-sop-1"] = SopDefinition(
        id="test-sop-1",
        name="Test",
        version="1.0",
        goal_template="Test",
        stages=[
            StageDefinition(
                id="stage-1",
                name="Test Stage",
                steps=[
                    StepDefinition(
                        id="step-1",
                        name="Step 1",
                        role_id="tester",
                        instructions="Test",
                        acceptance_criteria=[],
                        execution_mode=ExecutionMode.SEQUENTIAL,
                        depends_on=[],
                        requires_review=False,
                    )
                ],
            )
        ],
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Start SOP
        response = await client.post(
            "/api/sop-runs",
            json={
                "goal_description": "Pagination test",
                "sop_definition_id": "test-sop-1",
            },
        )
        sop_run_id = response.json()["sop_run_id"]

        # Get all events
        response = await client.get(f"/api/sop-runs/{sop_run_id}/events")
        all_events = response.json()
        assert len(all_events) > 0

        # Get events after sequence 1
        response = await client.get(f"/api/sop-runs/{sop_run_id}/events?after=1")
        filtered_events = response.json()
        assert len(filtered_events) < len(all_events)
        assert all(e["sequence"] > 1 for e in filtered_events)
