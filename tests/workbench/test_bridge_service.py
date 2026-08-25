"""Tests for Bridge service layer."""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, Mock
from workbench.backend.bridge.service import BridgeService, BridgeError
from workbench.backend.bridge.api_models import (
    ExecuteStepRequest,
    ExecuteStepResponse,
    TaskStatus,
    TaskStatusResponse,
)
from workbench.backend.domain.models import (
    Task,
    SubagentAssignment,
    Artifact,
    ContextPackage,
)


@pytest.fixture
def mock_runner():
    """Create mock RuntimeNeutralRunner."""
    runner = AsyncMock()
    runner.execute = AsyncMock()
    return runner


@pytest.fixture
def bridge_service(mock_runner):
    """Create BridgeService with mock dependencies."""
    return BridgeService(runner=mock_runner)


@pytest.mark.asyncio
class TestBridgeServiceExecuteStep:
    """Test BridgeService.execute_step()."""

    async def test_accepts_request_and_returns_running_response(
        self, bridge_service, mock_runner
    ):
        """Should accept ExecuteStepRequest and return running task."""
        request = ExecuteStepRequest(
            workflow_run_id="dify-run-123",
            node_id="researcher-node",
            role="security_researcher",
            capability="research_oauth2",
            goal="Research OAuth2 best practices",
            runtime_kind="claude_code",
        )

        # Mock runner to simulate async execution
        mock_runner.execute.return_value = Artifact(
            id="artifact-789",
            task_id="task-456",
            type="text",
            content="OAuth2 recommendations",
            summary="Research complete",
        )

        response = await bridge_service.execute_step(request)

        assert isinstance(response, ExecuteStepResponse)
        assert response.status == TaskStatus.RUNNING
        assert response.task_id is not None
        assert response.polling_url == f"/api/bridge/tasks/{response.task_id}"
        assert response.artifact is None
        assert response.error is None

    async def test_spawns_background_task(self, bridge_service, mock_runner):
        """Should spawn background task and not block."""
        request = ExecuteStepRequest(
            workflow_run_id="dify-run-123",
            node_id="researcher-node",
            role="security_researcher",
            capability="research_oauth2",
            goal="Research OAuth2 best practices",
            runtime_kind="claude_code",
        )

        # Mock runner should not be awaited during execute_step
        mock_runner.execute.return_value = Artifact(
            id="artifact-789",
            task_id="task-456",
            type="text",
            content="Result",
            summary="Done",
        )

        response = await bridge_service.execute_step(request)

        # Should return immediately with RUNNING status
        assert response.status == TaskStatus.RUNNING
        # Runner should not be called yet (background task)
        assert mock_runner.execute.call_count == 0

    async def test_converts_request_to_task_and_context(self, bridge_service):
        """Should convert ExecuteStepRequest to Task and ContextPackage."""
        request = ExecuteStepRequest(
            workflow_run_id="dify-run-123",
            node_id="researcher-node",
            role="security_researcher",
            capability="research_oauth2",
            goal="Research OAuth2 best practices",
            instructions="Focus on PKCE",
            constraints=["max 1000 tokens"],
            upstream_artifacts=[{"id": "art-1", "content": "Previous findings"}],
            runtime_kind="claude_code",
            workspace_scope="/project/auth",
        )

        response = await bridge_service.execute_step(request)

        # Verify task was stored internally
        task_id = response.task_id
        stored_task = bridge_service._tasks.get(task_id)

        assert stored_task is not None
        assert stored_task["request"] == request
        assert stored_task["status"] == TaskStatus.RUNNING
        assert stored_task["created_at"] is not None


@pytest.mark.asyncio
class TestBridgeServiceGetTaskStatus:
    """Test BridgeService.get_task_status()."""

    async def test_returns_running_status_for_active_task(self, bridge_service):
        """Should return RUNNING status for active task."""
        request = ExecuteStepRequest(
            workflow_run_id="dify-run-123",
            node_id="researcher-node",
            role="security_researcher",
            capability="research_oauth2",
            goal="Research OAuth2",
            runtime_kind="claude_code",
        )
        execute_response = await bridge_service.execute_step(request)
        task_id = execute_response.task_id

        status = await bridge_service.get_task_status(task_id)

        assert isinstance(status, TaskStatusResponse)
        assert status.task_id == task_id
        assert status.status == TaskStatus.RUNNING
        assert status.artifact is None
        assert status.error is None

    async def test_returns_completed_status_with_artifact(self, bridge_service):
        """Should return COMPLETED status with artifact when task finishes."""
        # Manually create a completed task
        task_id = "task-completed-123"
        artifact = Artifact(
            id="artifact-789",
            task_id=task_id,
            type="text",
            content="OAuth2 recommendations",
            summary="Research complete",
        )
        bridge_service._tasks[task_id] = {
            "status": TaskStatus.COMPLETED,
            "created_at": datetime(2026, 8, 25, 10, 30, 0),
            "updated_at": datetime(2026, 8, 25, 10, 31, 0),
            "artifact": artifact,
            "error": None,
        }

        status = await bridge_service.get_task_status(task_id)

        assert status.status == TaskStatus.COMPLETED
        assert status.artifact is not None
        assert status.artifact.id == "artifact-789"
        assert status.artifact.summary == "Research complete"

    async def test_returns_failed_status_with_error(self, bridge_service):
        """Should return FAILED status with error message."""
        task_id = "task-failed-123"
        bridge_service._tasks[task_id] = {
            "status": TaskStatus.FAILED,
            "created_at": datetime(2026, 8, 25, 10, 30, 0),
            "updated_at": datetime(2026, 8, 25, 10, 30, 15),
            "artifact": None,
            "error": "Runtime execution timeout after 30s",
        }

        status = await bridge_service.get_task_status(task_id)

        assert status.status == TaskStatus.FAILED
        assert status.error == "Runtime execution timeout after 30s"
        assert status.artifact is None

    async def test_raises_error_for_unknown_task_id(self, bridge_service):
        """Should raise BridgeError for unknown task_id."""
        with pytest.raises(BridgeError, match="Task not found: unknown-task-id"):
            await bridge_service.get_task_status("unknown-task-id")


@pytest.mark.asyncio
class TestBridgeServiceBackgroundExecution:
    """Test background task execution flow."""

    async def test_background_task_updates_status_on_completion(
        self, bridge_service, mock_runner
    ):
        """Should update task status to COMPLETED when runner finishes."""
        request = ExecuteStepRequest(
            workflow_run_id="dify-run-123",
            node_id="researcher-node",
            role="security_researcher",
            capability="research_oauth2",
            goal="Research OAuth2",
            runtime_kind="claude_code",
        )

        artifact = Artifact(
            id="artifact-789",
            task_id="task-456",
            type="text",
            content="OAuth2 recommendations",
            summary="Research complete",
        )
        mock_runner.execute.return_value = artifact

        execute_response = await bridge_service.execute_step(request)
        task_id = execute_response.task_id

        # Wait for background task to complete
        await bridge_service._wait_for_task(task_id)

        status = await bridge_service.get_task_status(task_id)
        assert status.status == TaskStatus.COMPLETED
        assert status.artifact is not None
        assert status.artifact.id == "artifact-789"

    async def test_background_task_updates_status_on_failure(
        self, bridge_service, mock_runner
    ):
        """Should update task status to FAILED when runner raises error."""
        request = ExecuteStepRequest(
            workflow_run_id="dify-run-123",
            node_id="researcher-node",
            role="security_researcher",
            capability="research_oauth2",
            goal="Research OAuth2",
            runtime_kind="claude_code",
        )

        mock_runner.execute.side_effect = Exception("Runtime execution failed")

        execute_response = await bridge_service.execute_step(request)
        task_id = execute_response.task_id

        # Wait for background task to complete
        await bridge_service._wait_for_task(task_id)

        status = await bridge_service.get_task_status(task_id)
        assert status.status == TaskStatus.FAILED
        assert "Runtime execution failed" in status.error
