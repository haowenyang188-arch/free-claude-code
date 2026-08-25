"""Pydantic models for Dify Bridge API."""

from enum import Enum
from datetime import datetime
from pydantic import BaseModel, Field, field_validator
from typing import Any


class TaskStatus(str, Enum):
    """Task execution status."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ExecuteStepRequest(BaseModel):
    """Request to execute a step via Dify Bridge."""

    workflow_run_id: str = Field(
        ..., description="Dify workflow run ID for correlation"
    )
    node_id: str = Field(..., description="Dify node ID that triggered this execution")
    role: str = Field(..., description="Agent role (e.g., security_researcher)")
    capability: str = Field(
        ..., description="Capability to execute (e.g., research_oauth2)"
    )
    goal: str = Field(..., description="Task goal/objective")
    instructions: str | None = Field(
        None, description="Optional detailed instructions"
    )
    constraints: list[str] = Field(
        default_factory=list, description="Execution constraints"
    )
    upstream_artifacts: list[dict[str, Any]] = Field(
        default_factory=list, description="Artifacts from upstream Dify nodes"
    )
    runtime_kind: str = Field(..., description="Runtime to use (claude_code, codex)")
    workspace_scope: str | None = Field(
        None, description="Optional workspace directory path"
    )

    @field_validator("runtime_kind")
    @classmethod
    def validate_runtime_kind(cls, v: str) -> str:
        """Validate runtime_kind is one of the supported runtimes."""
        valid_runtimes = {"claude_code", "codex"}
        if v not in valid_runtimes:
            raise ValueError(
                f"Invalid runtime_kind: {v}. Must be one of {valid_runtimes}"
            )
        return v


class ArtifactResponse(BaseModel):
    """Artifact produced by task execution."""

    id: str = Field(..., description="Artifact ID (content-addressed SHA-256)")
    type: str = Field(..., description="Artifact type (text, code, file)")
    content: str = Field(..., description="Artifact content")
    summary: str = Field(..., description="Human-readable summary")


class ExecuteStepResponse(BaseModel):
    """Response from execute_step endpoint."""

    task_id: str = Field(..., description="Task ID for polling status")
    status: TaskStatus = Field(..., description="Current task status")
    polling_url: str = Field(..., description="URL to poll for task status")
    artifact: ArtifactResponse | None = Field(
        None, description="Artifact if task completed"
    )
    error: str | None = Field(None, description="Error message if task failed")


class TaskStatusResponse(BaseModel):
    """Response from task status polling endpoint."""

    task_id: str = Field(..., description="Task ID")
    status: TaskStatus = Field(..., description="Current task status")
    created_at: datetime = Field(..., description="Task creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    artifact: ArtifactResponse | None = Field(
        None, description="Artifact if task completed"
    )
    error: str | None = Field(None, description="Error message if task failed")
