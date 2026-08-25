"""FastAPI routes for Dify Bridge."""

from fastapi import APIRouter, HTTPException, status
from workbench.backend.bridge.api_models import (
    ExecuteStepRequest,
    ExecuteStepResponse,
    TaskStatusResponse,
)
from workbench.backend.bridge.service import BridgeService, BridgeError


def create_bridge_router(bridge_service: BridgeService) -> APIRouter:
    """Create FastAPI router for Bridge endpoints."""
    router = APIRouter(prefix="/api/bridge", tags=["bridge"])

    @router.post(
        "/execute_step",
        response_model=ExecuteStepResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def execute_step(request: ExecuteStepRequest) -> ExecuteStepResponse:
        """
        Execute a step via Dify Bridge.

        Accepts step execution request from Dify and spawns background task.
        Returns immediately with RUNNING status and polling URL.
        """
        return await bridge_service.execute_step(request)

    @router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
    async def get_task_status(task_id: str) -> TaskStatusResponse:
        """
        Poll task status by task_id.

        Returns current status, artifact if completed, or error if failed.
        """
        try:
            return await bridge_service.get_task_status(task_id)
        except BridgeError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
            ) from e

    @router.get("/health")
    async def health() -> dict:
        """Health check endpoint."""
        return {"status": "healthy", "bridge_version": "1.0.0"}

    return router
