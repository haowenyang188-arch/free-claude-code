"""Tests for Dify Bridge API endpoints."""

import pytest
from datetime import datetime
from workbench.backend.bridge.api_models import (
    ExecuteStepRequest,
    ExecuteStepResponse,
    TaskStatus,
    TaskStatusResponse,
    ArtifactResponse,
)


class TestExecuteStepRequest:
    """Test ExecuteStepRequest validation and parsing."""

    def test_minimal_valid_request(self):
        """Should parse minimal valid request."""
        data = {
            "workflow_run_id": "dify-run-123",
            "node_id": "researcher-node",
            "role": "security_researcher",
            "capability": "research_oauth2",
            "goal": "Research OAuth2 best practices",
            "runtime_kind": "claude_code",
        }
        req = ExecuteStepRequest(**data)

        assert req.workflow_run_id == "dify-run-123"
        assert req.node_id == "researcher-node"
        assert req.role == "security_researcher"
        assert req.capability == "research_oauth2"
        assert req.goal == "Research OAuth2 best practices"
        assert req.runtime_kind == "claude_code"
        assert req.instructions is None
        assert req.constraints == []
        assert req.upstream_artifacts == []
        assert req.workspace_scope is None

    def test_full_request_with_all_fields(self):
        """Should parse request with all optional fields."""
        data = {
            "workflow_run_id": "dify-run-123",
            "node_id": "researcher-node",
            "role": "security_researcher",
            "capability": "research_oauth2",
            "goal": "Research OAuth2 best practices",
            "instructions": "Focus on PKCE and token storage",
            "constraints": ["max 1000 tokens", "use only public sources"],
            "upstream_artifacts": [
                {"id": "artifact-1", "content": "Previous findings"}
            ],
            "runtime_kind": "claude_code",
            "workspace_scope": "/project/auth",
        }
        req = ExecuteStepRequest(**data)

        assert req.instructions == "Focus on PKCE and token storage"
        assert len(req.constraints) == 2
        assert req.constraints[0] == "max 1000 tokens"
        assert len(req.upstream_artifacts) == 1
        assert req.upstream_artifacts[0]["id"] == "artifact-1"
        assert req.workspace_scope == "/project/auth"

    def test_rejects_missing_required_fields(self):
        """Should reject request missing required fields."""
        data = {
            "workflow_run_id": "dify-run-123",
            # Missing node_id, role, capability, goal, runtime_kind
        }
        with pytest.raises(Exception):  # pydantic ValidationError
            ExecuteStepRequest(**data)

    def test_rejects_invalid_runtime_kind(self):
        """Should reject unknown runtime_kind."""
        data = {
            "workflow_run_id": "dify-run-123",
            "node_id": "researcher-node",
            "role": "security_researcher",
            "capability": "research_oauth2",
            "goal": "Research OAuth2 best practices",
            "runtime_kind": "invalid_runtime",
        }
        with pytest.raises(ValueError, match="Invalid runtime_kind"):
            ExecuteStepRequest(**data)


class TestExecuteStepResponse:
    """Test ExecuteStepResponse serialization."""

    def test_creates_response_with_running_status(self):
        """Should create response for running task."""
        resp = ExecuteStepResponse(
            task_id="task-456",
            status=TaskStatus.RUNNING,
            polling_url="/api/bridge/tasks/task-456",
        )

        assert resp.task_id == "task-456"
        assert resp.status == TaskStatus.RUNNING
        assert resp.polling_url == "/api/bridge/tasks/task-456"
        assert resp.artifact is None
        assert resp.error is None

    def test_creates_response_with_completed_status(self):
        """Should create response for completed task."""
        artifact = ArtifactResponse(
            id="artifact-789",
            type="text",
            content="OAuth2 recommendations:\n1. Use PKCE\n2. ...",
            summary="OAuth2 research complete",
        )
        resp = ExecuteStepResponse(
            task_id="task-456",
            status=TaskStatus.COMPLETED,
            polling_url="/api/bridge/tasks/task-456",
            artifact=artifact,
        )

        assert resp.status == TaskStatus.COMPLETED
        assert resp.artifact is not None
        assert resp.artifact.id == "artifact-789"
        assert resp.artifact.summary == "OAuth2 research complete"

    def test_creates_response_with_failed_status(self):
        """Should create response for failed task."""
        resp = ExecuteStepResponse(
            task_id="task-456",
            status=TaskStatus.FAILED,
            polling_url="/api/bridge/tasks/task-456",
            error="Runtime execution failed: timeout",
        )

        assert resp.status == TaskStatus.FAILED
        assert resp.error == "Runtime execution failed: timeout"
        assert resp.artifact is None


class TestTaskStatusResponse:
    """Test TaskStatusResponse serialization."""

    def test_running_task_status(self):
        """Should represent running task status."""
        resp = TaskStatusResponse(
            task_id="task-456",
            status=TaskStatus.RUNNING,
            created_at=datetime(2026, 8, 25, 10, 30, 0),
            updated_at=datetime(2026, 8, 25, 10, 30, 5),
        )

        assert resp.task_id == "task-456"
        assert resp.status == TaskStatus.RUNNING
        assert resp.artifact is None
        assert resp.error is None

    def test_completed_task_status(self):
        """Should represent completed task status with artifact."""
        artifact = ArtifactResponse(
            id="artifact-789",
            type="text",
            content="Result content",
            summary="Task completed",
        )
        resp = TaskStatusResponse(
            task_id="task-456",
            status=TaskStatus.COMPLETED,
            created_at=datetime(2026, 8, 25, 10, 30, 0),
            updated_at=datetime(2026, 8, 25, 10, 31, 0),
            artifact=artifact,
        )

        assert resp.status == TaskStatus.COMPLETED
        assert resp.artifact is not None
        assert resp.artifact.id == "artifact-789"

    def test_failed_task_status(self):
        """Should represent failed task status with error."""
        resp = TaskStatusResponse(
            task_id="task-456",
            status=TaskStatus.FAILED,
            created_at=datetime(2026, 8, 25, 10, 30, 0),
            updated_at=datetime(2026, 8, 25, 10, 30, 15),
            error="Execution timeout after 30s",
        )

        assert resp.status == TaskStatus.FAILED
        assert resp.error == "Execution timeout after 30s"
        assert resp.artifact is None
