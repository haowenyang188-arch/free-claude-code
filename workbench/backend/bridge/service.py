"""Bridge service layer for Dify integration."""

import asyncio
import uuid
from datetime import datetime, UTC
from typing import Any
from workbench.backend.bridge.api_models import (
    ExecuteStepRequest,
    ExecuteStepResponse,
    TaskStatus,
    TaskStatusResponse,
    ArtifactResponse,
)
from workbench.backend.domain.models import (
    Task,
    SubagentAssignment,
    Artifact,
    ContextPackage,
)
from workbench.backend.workflow.runners import RuntimeNeutralRunner


class BridgeError(Exception):
    """Raised when bridge operation fails."""


class BridgeService:
    """Service layer for Dify Bridge - manages async task execution."""

    def __init__(self, runner: RuntimeNeutralRunner) -> None:
        self._runner = runner
        self._tasks: dict[str, dict[str, Any]] = {}
        self._background_tasks: dict[str, asyncio.Task] = {}

    async def execute_step(self, request: ExecuteStepRequest) -> ExecuteStepResponse:
        """
        Accept step execution request from Dify and spawn background task.

        Returns immediately with RUNNING status and polling URL.
        """
        task_id = self._generate_task_id()

        # Store task metadata
        self._tasks[task_id] = {
            "request": request,
            "status": TaskStatus.RUNNING,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            "artifact": None,
            "error": None,
        }

        # Spawn background task
        background_task = asyncio.create_task(
            self._execute_in_background(task_id, request)
        )
        self._background_tasks[task_id] = background_task

        return ExecuteStepResponse(
            task_id=task_id,
            status=TaskStatus.RUNNING,
            polling_url=f"/api/bridge/tasks/{task_id}",
        )

    async def get_task_status(self, task_id: str) -> TaskStatusResponse:
        """Poll task status by task_id."""
        task = self._tasks.get(task_id)
        if task is None:
            raise BridgeError(f"Task not found: {task_id}")

        artifact_response = None
        if task["artifact"] is not None:
            artifact = task["artifact"]
            artifact_response = ArtifactResponse(
                id=artifact.id,
                type=artifact.type,
                content=artifact.content,
                summary=artifact.summary,
            )

        return TaskStatusResponse(
            task_id=task_id,
            status=task["status"],
            created_at=task["created_at"],
            updated_at=task["updated_at"],
            artifact=artifact_response,
            error=task["error"],
        )

    async def _execute_in_background(
        self, task_id: str, request: ExecuteStepRequest
    ) -> None:
        """Execute task in background and update status."""
        try:
            # Convert ExecuteStepRequest to Task and ContextPackage
            task = self._request_to_task(task_id, request)
            context = self._request_to_context(task_id, request)
            assignment = self._request_to_assignment(task_id, request)

            # Execute through RuntimeNeutralRunner
            artifact = await self._runner.execute(
                task=task, assignment=assignment, context=context
            )

            # Update task status to COMPLETED
            self._tasks[task_id].update(
                {
                    "status": TaskStatus.COMPLETED,
                    "updated_at": datetime.now(UTC),
                    "artifact": artifact,
                }
            )

        except Exception as e:
            # Update task status to FAILED
            self._tasks[task_id].update(
                {
                    "status": TaskStatus.FAILED,
                    "updated_at": datetime.now(UTC),
                    "error": str(e),
                }
            )

    def _request_to_task(self, task_id: str, request: ExecuteStepRequest) -> Task:
        """Convert ExecuteStepRequest to Task domain model."""
        # Generate synthetic step_run_id from workflow_run_id + node_id
        step_run_id = f"{request.workflow_run_id}-{request.node_id}"

        return Task(
            id=task_id,
            step_run_id=step_run_id,
            title=request.goal,
            description=request.instructions or "",
            role_id=request.role,
            status="pending",
        )

    def _request_to_assignment(
        self, task_id: str, request: ExecuteStepRequest
    ) -> SubagentAssignment:
        """Convert ExecuteStepRequest to SubagentAssignment."""
        # Generate synthetic IDs for Bridge execution
        assignment_id = f"assign-{uuid.uuid4().hex[:12]}"
        agent_instance_id = f"bridge-agent-{request.runtime_kind}"

        return SubagentAssignment(
            id=assignment_id,
            task_id=task_id,
            role_id=request.role,
            agent_instance_id=agent_instance_id,
            runtime_id=request.runtime_kind,
        )

    def _request_to_context(
        self, task_id: str, request: ExecuteStepRequest
    ) -> ContextPackage:
        """Convert ExecuteStepRequest to ContextPackage."""
        # Extract artifact IDs from upstream_artifacts
        upstream_artifact_ids = [
            art["id"] for art in request.upstream_artifacts if "id" in art
        ]

        # Generate synthetic context package ID
        context_id = f"ctx-{uuid.uuid4().hex[:12]}"

        return ContextPackage(
            id=context_id,
            task_id=task_id,
            goal_summary=request.goal,
            instructions=request.instructions or "",
            constraints=request.constraints,
            artifact_ids=upstream_artifact_ids,
            decisions=[],
            allowed_tools=[],
            workspace_scope=request.workspace_scope,
        )

    def _generate_task_id(self) -> str:
        """Generate unique task ID."""
        return f"task-{uuid.uuid4().hex[:12]}"

    async def _wait_for_task(self, task_id: str) -> None:
        """Wait for background task to complete (test helper)."""
        background_task = self._background_tasks.get(task_id)
        if background_task:
            await background_task
