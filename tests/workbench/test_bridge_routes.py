"""Tests for Bridge FastAPI routes."""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch
from workbench.backend.bridge.api_models import (
    ExecuteStepRequest,
    ExecuteStepResponse,
    TaskStatus,
    TaskStatusResponse,
)
from workbench.backend.bridge.routes import create_bridge_router


@pytest.fixture
def mock_bridge_service():
    """Create mock BridgeService."""
    service = AsyncMock()
    return service


@pytest.fixture
def client(mock_bridge_service):
    """Create FastAPI test client with mocked service."""
    router = create_bridge_router(bridge_service=mock_bridge_service)
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    return TestClient(app), mock_bridge_service


class TestExecuteStepEndpoint:
    """Test POST /api/bridge/execute_step endpoint."""

    def test_accepts_valid_request_and_returns_202(self, client):
        """Should accept valid request and return 202 with task info."""
        test_client, mock_service = client

        mock_service.execute_step.return_value = ExecuteStepResponse(
            task_id="task-123",
            status=TaskStatus.RUNNING,
            polling_url="/api/bridge/tasks/task-123",
        )

        response = test_client.post(
            "/api/bridge/execute_step",
            json={
                "workflow_run_id": "dify-run-123",
                "node_id": "researcher-node",
                "role": "security_researcher",
                "capability": "research_oauth2",
                "goal": "Research OAuth2 best practices",
                "runtime_kind": "claude_code",
            },
        )

        assert response.status_code == 202
        data = response.json()
        assert data["task_id"] == "task-123"
        assert data["status"] == "running"
        assert data["polling_url"] == "/api/bridge/tasks/task-123"

    def test_rejects_invalid_runtime_kind(self, client):
        """Should return 422 for invalid runtime_kind."""
        test_client, _ = client

        response = test_client.post(
            "/api/bridge/execute_step",
            json={
                "workflow_run_id": "dify-run-123",
                "node_id": "researcher-node",
                "role": "security_researcher",
                "capability": "research_oauth2",
                "goal": "Research OAuth2",
                "runtime_kind": "invalid_runtime",
            },
        )

        assert response.status_code == 422
        assert "runtime_kind" in response.text.lower()

    def test_rejects_missing_required_fields(self, client):
        """Should return 422 for missing required fields."""
        test_client, _ = client

        response = test_client.post(
            "/api/bridge/execute_step",
            json={
                "workflow_run_id": "dify-run-123",
                # Missing node_id, role, capability, goal, runtime_kind
            },
        )

        assert response.status_code == 422


class TestGetTaskStatusEndpoint:
    """Test GET /api/bridge/tasks/{task_id} endpoint."""

    def test_returns_running_status(self, client):
        """Should return 200 with running status."""
        test_client, mock_service = client

        from datetime import datetime, UTC

        mock_service.get_task_status.return_value = TaskStatusResponse(
            task_id="task-123",
            status=TaskStatus.RUNNING,
            created_at=datetime(2026, 8, 25, 10, 30, 0, tzinfo=UTC),
            updated_at=datetime(2026, 8, 25, 10, 30, 5, tzinfo=UTC),
        )

        response = test_client.get("/api/bridge/tasks/task-123")

        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == "task-123"
        assert data["status"] == "running"
        assert data["artifact"] is None

    def test_returns_completed_status_with_artifact(self, client):
        """Should return 200 with completed status and artifact."""
        test_client, mock_service = client

        from datetime import datetime, UTC
        from workbench.backend.bridge.api_models import ArtifactResponse

        artifact = ArtifactResponse(
            id="artifact-789",
            type="text",
            content="OAuth2 recommendations",
            summary="Research complete",
        )

        mock_service.get_task_status.return_value = TaskStatusResponse(
            task_id="task-123",
            status=TaskStatus.COMPLETED,
            created_at=datetime(2026, 8, 25, 10, 30, 0, tzinfo=UTC),
            updated_at=datetime(2026, 8, 25, 10, 31, 0, tzinfo=UTC),
            artifact=artifact,
        )

        response = test_client.get("/api/bridge/tasks/task-123")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["artifact"] is not None
        assert data["artifact"]["id"] == "artifact-789"

    def test_returns_404_for_unknown_task(self, client):
        """Should return 404 for unknown task_id."""
        test_client, mock_service = client

        from workbench.backend.bridge.service import BridgeError

        mock_service.get_task_status.side_effect = BridgeError(
            "Task not found: unknown-task"
        )

        response = test_client.get("/api/bridge/tasks/unknown-task")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


class TestHealthEndpoint:
    """Test GET /api/bridge/health endpoint."""

    def test_returns_200_with_healthy_status(self, client):
        """Should return 200 with healthy status."""
        test_client, _ = client

        response = test_client.get("/api/bridge/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "bridge_version" in data
