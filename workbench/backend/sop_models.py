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
    auto_execute: bool = False  # If True, automatically trigger execution after creation


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
    metadata: dict[str, Any] = Field(default_factory=dict)


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
    # Phase 5A: Attempt lineage support
    attempt_id: str | None = None
    # producer_step_run_id 是 Artifact 自身字段（domain/models.py:362），
    # 不是路由投影；schema-v1 旧记录为 None。
    producer_step_run_id: str | None = None
    # role_id 不是 Artifact 自身字段，是路由层从 Task.role_id join 出来的投影
    # （Artifact 模型上不存在 role / role_id），旧记录可能为 None。
    role_id: str | None = None


class HandoffResponse(BaseModel):
    """Read-only Handoff projection for the collaboration console."""

    id: str
    from_task_id: str
    to_step_id: str
    status: str
    artifact_ids: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    accepted_at: datetime | None = None
    message_type: str = "handoff"
    brief: str = ""
    reply_to_handoff_id: str | None = None
    correlation_id: str | None = None
    # Canonical collaboration-audit names.  Optional defaults preserve old
    # persisted Handoff records that predate role/runtime projections.
    source_role_id: str | None = None
    target_role_id: str | None = None
    target_runtime_id: str | None = None
    source_runtime_id: str | None = None
    # Transitional aliases retained for an already-shipped console build.
    from_role_id: str | None = None
    to_role_id: str | None = None
    dispatchable: bool = False
    dispatch_reason: str | None = None


class SopHandoffDispatchResponse(BaseModel):
    """Auditable result of one Engine-owned Handoff dispatch."""

    sop_run_id: str
    handoff_id: str
    status: str
    target_step_id: str
    target_role_id: str
    target_runtime_id: str
    runtime_mode: str
    task_id: str
    attempt_id: str | None = None
    # ``workflow_session_id`` is the Workbench Session record. ``session_id``
    # is the runtime's returned session id when the adapter provides one.
    workflow_session_id: str | None = None
    session_id: str | None = None
    event_id: str
    outgoing_handoff_id: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)


class SopTraceEntryResponse(BaseModel):
    """Sanitized workflow observation; intentionally excludes raw payloads."""

    sequence: int
    event_type: str
    role_id: str | None = None
    runtime_id: str | None = None
    phase: str
    message: str
    tool: str | None = None
    status: str
    occurred_at: datetime


class SopTraceResponse(BaseModel):
    """Read-only SOP trace with explicit provider-event availability."""

    sop_run_id: str
    mode: str
    provider_events_available: bool
    empty: bool
    entries: list[SopTraceEntryResponse] = Field(default_factory=list)


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


class AttemptResponse(BaseModel):
    """Attempt projection - read-only view with computed lineage."""

    id: str
    task_id: str
    sequence: int
    session_id: str | None = None
    runtime_id: str | None = None
    previous_attempt_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    status: str
    # Computed fields
    sop_run_id: str  # Resolved via Task -> StepRun -> SopRun
    artifact_ids: list[str] = Field(default_factory=list)  # Explicit query


class ReviewEvidenceResponse(BaseModel):
    """Review evidence triple projection with validation.

    outcome / blocking_items 解析自 reviewer_report Artifact 的 JSON content
    （result / blocking 字段，容错语义与 engine 的评审反馈解析一致），不是
    Review 模型上的列——Review 只有 status / feedback / reviewer_role_id。
    解析失败时必须显式返回 outcome=None + outcome_parse_error，绝不伪造 PASS。
    """

    review_id: str
    # Execution Attempt being reviewed（schema-v2 前记录可能为 None）
    reviewed_attempt_id: str | None = None
    # Review.status（pending / approved / rejected / changes_requested）
    review_status: str
    # 解析自 reviewer_report.content 的 result（PASS / REWORK / PLAN_INVALID）
    outcome: str | None = None
    blocking_items: list[str] = Field(default_factory=list)  # 同上，parse 失败则为空
    outcome_parse_error: str | None = None  # 解析失败原因，供前端显式降级
    # Execution Attempt outputs: DIFF + TEST_REPORT
    execution_diff: ArtifactResponse | None = None
    execution_test_report: ArtifactResponse | None = None
    # Reviewer Attempt output: REVIEW_REPORT
    reviewer_report: ArtifactResponse | None = None
    reviewer_attempt_id: str | None = None
    # Validation results
    evidence_valid: bool  # DIFF and TEST_REPORT both from reviewed_attempt_id
    evidence_complete: bool  # All three artifacts exist
    created_at: datetime


class AttemptChainResponse(BaseModel):
    """REWORK retry chain projection via previous_attempt_id."""

    attempts: list[AttemptResponse]  # Ordered by previous_attempt_id, not sequence
    root_task_id: str
    chain_length: int
    truncated: bool = False  # previous_attempt_id 成环时被截断，前端需显式提示
