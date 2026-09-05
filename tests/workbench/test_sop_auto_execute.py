"""Test auto-execute functionality for SOP runs."""

import pytest
from httpx import ASGITransport, AsyncClient

from workbench.backend.agents.fake_runner import FakeSubagentRunner
from workbench.backend.artifacts.store import FileArtifactStore
from workbench.backend.domain.models import (
    AcceptanceCriteria,
    SopDefinition,
    SopOrigin,
    StageDefinition,
    StepDefinition,
)
from workbench.backend.main import app, service
from workbench.backend.persistence.store import JsonWorkflowStore
from workbench.backend.validation.always_accept import AlwaysAcceptValidator
from workbench.backend.workflow.context_builder import ContextPackageBuilder
from workbench.backend.workflow.engine import WorkflowEngine
from workbench.backend.workflow.orchestrator import AutoOrchestrator


@pytest.fixture
def isolated_components(tmp_path):
    """Create isolated components for testing."""

    # Save original
    original_store = service.workflow_store
    original_artifact = service.artifact_store
    original_engine = service.workflow_engine
    original_context = service.context_builder
    original_orchestrator = service.orchestrator
    original_defs = service.sop_definitions
    original_tasks = service._background_sop_tasks

    # Create isolated
    workspace = tmp_path / "test_workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    service.workflow_store = JsonWorkflowStore(
        state_path=workspace / "state.json",
        event_path=workspace / "events.jsonl",
    )
    service.artifact_store = FileArtifactStore(root=workspace / "artifacts")

    runner = FakeSubagentRunner(artifact_content="Auto-executed result")
    validator = AlwaysAcceptValidator()

    service.workflow_engine = WorkflowEngine(
        store=service.workflow_store,
        runner=runner,
        validator=validator,
        artifact_store=service.artifact_store,
    )
    service.context_builder = ContextPackageBuilder(
        artifact_store=service.artifact_store, store=service.workflow_store
    )
    service.orchestrator = AutoOrchestrator(engine=service.workflow_engine)

    # Clear existing dicts (don't replace - keep same object reference)
    service.sop_definitions.clear()
    service._background_sop_tasks.clear()

    # Test SOP
    sop = SopDefinition(
        id="test-auto-exec",
        name="Auto Execute Test",
        version=1,
        origin=SopOrigin.FIXED,
        stages=[
            StageDefinition(
                id="stage-1",
                name="Test Stage",
                steps=[
                    StepDefinition(
                        id="step-1",
                        name="Test Step",
                        role_id="executor",
                        instructions="Execute this",
                        acceptance_criteria=[
                            AcceptanceCriteria(
                                id="ac-1", description="Task done", required=True
                            )
                        ],
                    )
                ],
            )
        ],
    )
    service.sop_definitions[sop.id] = sop

    yield service

    # Restore
    service.workflow_store = original_store
    service.artifact_store = original_artifact
    service.workflow_engine = original_engine
    service.context_builder = original_context
    service.orchestrator = original_orchestrator
    service.sop_definitions = original_defs
    service._background_sop_tasks = original_tasks


@pytest.mark.asyncio
async def test_post_without_auto_execute(isolated_components):
    """POST /api/sop-runs without auto_execute should only create state."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/sop-runs",
            json={
                "goal_description": "Test goal",
                "sop_definition_id": "test-auto-exec",
                "project_id": "test-project",
                "auto_execute": False,
            },
        )

        assert response.status_code == 200
        data = response.json()
        run_id = data["sop_run_id"]

        # Check run status - should be RUNNING (state created, no tasks yet)
        status_response = await client.get(f"/api/sop-runs/{run_id}")
        assert status_response.status_code == 200
        status_data = status_response.json()
        assert status_data["status"] == "running"

        # Check tasks - should be empty (no execution triggered)
        tasks_response = await client.get(f"/api/sop-runs/{run_id}/tasks")
        assert tasks_response.status_code == 200
        tasks = tasks_response.json()
        assert len(tasks) == 0


@pytest.mark.asyncio
async def test_post_preserves_run_metadata(isolated_components):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/sop-runs",
            json={
                "goal_description": "Canvas-linked goal",
                "sop_definition_id": "test-auto-exec",
                "metadata": {"canvas_acp_session_id": "acp-session-1"},
            },
        )

        assert response.status_code == 200
        run_id = response.json()["sop_run_id"]
        status_response = await client.get(f"/api/sop-runs/{run_id}")
        assert status_response.status_code == 200
        assert status_response.json()["metadata"] == {
            "canvas_acp_session_id": "acp-session-1"
        }


@pytest.mark.asyncio
async def test_post_with_auto_execute(isolated_components):
    """POST /api/sop-runs with auto_execute=True should trigger execution."""
    import asyncio

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/sop-runs",
            json={
                "goal_description": "Test goal",
                "sop_definition_id": "test-auto-exec",
                "project_id": "test-project",
                "auto_execute": True,
            },
        )

        assert response.status_code == 200
        data = response.json()
        run_id = data["sop_run_id"]

        # Check immediately that task was created (before it completes and cleans up)
        from workbench.backend.main import service as global_service

        # The task should be in the dict right after creation
        if run_id in global_service._background_sop_tasks:
            task = global_service._background_sop_tasks[run_id]
            # Wait for it to complete
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except TimeoutError:
                pytest.fail("Background task did not complete within timeout")
            except Exception as e:
                pytest.fail(f"Background task failed with: {e}")
        else:
            # If not found immediately, wait a bit and check again
            await asyncio.sleep(0.1)
            if run_id in global_service._background_sop_tasks:
                task = global_service._background_sop_tasks[run_id]
                await asyncio.wait_for(task, timeout=5.0)
            else:
                # Task already completed and cleaned up - that's actually fine for a fast runner
                pass

        # Check tasks - should have been created and executed
        tasks_response = await client.get(f"/api/sop-runs/{run_id}/tasks")
        assert tasks_response.status_code == 200
        tasks = tasks_response.json()
        assert len(tasks) == 1
        assert tasks[0]["status"] == "accepted"


@pytest.mark.asyncio
async def test_execute_endpoint(isolated_components):
    """POST /api/sop-runs/{run_id}/execute should trigger orchestration."""
    import asyncio

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Create run without auto_execute
        response = await client.post(
            "/api/sop-runs",
            json={
                "goal_description": "Test goal",
                "sop_definition_id": "test-auto-exec",
                "project_id": "test-project",
                "auto_execute": False,
            },
        )

        assert response.status_code == 200
        run_id = response.json()["sop_run_id"]

        # Trigger execution via /execute endpoint
        exec_response = await client.post(f"/api/sop-runs/{run_id}/execute")
        assert exec_response.status_code == 200
        exec_data = exec_response.json()
        assert exec_data["status"] == "executing"

        # Wait for background task to complete
        await asyncio.sleep(0.1)

        from workbench.backend.main import service

        if run_id in service._background_sop_tasks:
            task = service._background_sop_tasks[run_id]
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except TimeoutError:
                pytest.fail("Background task did not complete within timeout")

        # Verify tasks were created and completed
        tasks_response = await client.get(f"/api/sop-runs/{run_id}/tasks")
        assert tasks_response.status_code == 200
        tasks = tasks_response.json()
        assert len(tasks) == 1
        assert tasks[0]["status"] == "accepted"


@pytest.mark.asyncio
async def test_execute_endpoint_404(isolated_components):
    """POST /api/sop-runs/{run_id}/execute should return 404 for invalid run_id."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/sop-runs/invalid-id/execute")
        assert response.status_code == 404
