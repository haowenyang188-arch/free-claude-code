"""SOP API request/response models (separated from domain models)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class StartSopRunRequest(BaseModel):
    """Request to start a new SOP run."""

    goal_description: str
    sop_definition_id: str
    project_id: str = "default"
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StartSopRunResponse(BaseModel):
    """Response from starting a SOP run."""

    sop_run_id: str
    goal_id: str
    status: str
    started_at: datetime


class SopRunStatusResponse(BaseModel):
    """SOP run status and summary."""

    id: str
    goal_id: str
    sop_definition_id: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    step_count: int = 0
    steps_completed: int = 0
    steps_ready: int = 0
    steps_running: int = 0


class StepRunResponse(BaseModel):
    """Step run status."""

    id: str
    sop_run_id: str
    step_id: str
    stage_run_id: str
    status: str
    task_id: str | None = None


class TaskResponse(BaseModel):
    """Task summary."""

    id: str
    step_run_id: str
    title: str
    description: str
    role_id: str
    status: str
    input_artifact_ids: list[str] = Field(default_factory=list)
    output_artifact_ids: list[str] = Field(default_factory=list)


class ArtifactResponse(BaseModel):
    """Artifact summary."""

    id: str
    task_id: str
    type: str
    uri: str | None = None
    summary: str | None = None
    sha256: str | None = None
    accepted: bool
    created_at: datetime


class HandoffResponse(BaseModel):
    """Handoff summary."""

    id: str
    from_task_id: str
    to_step_id: str
    status: str
    artifact_ids: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    accepted_at: datetime | None = None


class SopEventResponse(BaseModel):
    """SOP event from event log."""

    id: str
    stream_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime


class SopControlRequest(BaseModel):
    """Control request for SOP run."""

    action: str  # "pause", "resume", "cancel"
    reason: str | None = None
