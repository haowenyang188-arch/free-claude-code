"""Stable V1 contracts for SOP-driven subagent execution.

The domain models deliberately describe orchestration state, not a particular
CLI. Runtime adapters remain behind the ``Runtime`` and ``Session`` records.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(UTC)


class SopOrigin(StrEnum):
    FIXED = "fixed"
    TEMPLATE = "template"
    AI_INSTANTIATED = "ai_instantiated"
    AI_REPLANNED = "ai_replanned"


class RuntimeKind(StrEnum):
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    DEEPSEEK_HARNESS = "deepseek_harness"
    ACP = "acp"
    CUSTOM = "custom"


class ExecutionMode(StrEnum):
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"


class GoalStatus(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class SopRunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_REVIEW = "waiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    VALIDATING = "validating"
    WAITING_REVIEW = "waiting_review"
    COMPLETED = "completed"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    FAILED = "failed"
    PAUSED = "paused"


class TaskStatus(StrEnum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    RUNNING = "running"
    VALIDATING = "validating"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REWORK = "rework"
    FAILED = "failed"


class ArtifactType(StrEnum):
    TEXT = "text"
    RESEARCH_REPORT = "research_report"
    ARCHITECTURE_SPEC = "architecture_spec"
    IMPLEMENTATION = "implementation"
    TEST_REPORT = "test_report"
    REVIEW_REPORT = "review_report"
    FINAL_SUMMARY = "final_summary"
    FILE = "file"
    DIFF = "diff"
    JSON = "json"


class ValidationStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ERROR = "error"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"


class HandoffStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class Project(BaseModel):
    id: str
    name: str
    workspace_path: str | None = None
    created_at: datetime = Field(default_factory=_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Goal(BaseModel):
    id: str
    project_id: str
    description: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    status: GoalStatus = GoalStatus.DRAFT
    created_at: datetime = Field(default_factory=_now)
    completed_at: datetime | None = None


class Role(BaseModel):
    id: str
    name: str
    capabilities: list[str] = Field(default_factory=list)
    instructions: str | None = None
    acceptance_policy: str | None = None


class AgentProfile(BaseModel):
    id: str
    name: str
    capabilities: list[str] = Field(default_factory=list)
    supported_roles: list[str] = Field(default_factory=list)
    runtime_id: str | None = None
    default_model: str | None = None
    permission_policy: str = "read-only"
    mcp_policy: list[str] = Field(default_factory=list)


class AgentInstance(BaseModel):
    id: str
    profile_id: str
    status: str = "offline"
    workspace_path: str | None = None
    current_session_id: str | None = None
    current_task_id: str | None = None
    last_heartbeat: datetime | None = None


class Runtime(BaseModel):
    id: str
    name: str
    kind: RuntimeKind
    capabilities: list[str] = Field(default_factory=list)
    endpoint: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Session(BaseModel):
    id: str
    runtime_id: str
    agent_instance_id: str
    external_id: str | None = None
    status: str = "created"
    workspace_path: str | None = None
    started_at: datetime = Field(default_factory=_now)
    ended_at: datetime | None = None


class RoleBinding(BaseModel):
    id: str
    role_id: str
    agent_instance_id: str
    runtime_id: str
    session_id: str | None = None


class StepDefinition(BaseModel):
    id: str
    name: str
    role_id: str
    output_type: ArtifactType = ArtifactType.TEXT
    depends_on: list[str] = Field(default_factory=list)
    execution_mode: ExecutionMode = ExecutionMode.SEQUENTIAL
    requires_review: bool = False
    validator_id: str | None = None
    retry_limit: int = 0
    handoff_to: str | None = None
    instructions: str = ""


class StageDefinition(BaseModel):
    id: str
    name: str
    steps: list[StepDefinition] = Field(default_factory=list)


class SopDefinition(BaseModel):
    id: str
    name: str
    version: int = 1
    origin: SopOrigin = SopOrigin.FIXED
    template_id: str | None = None
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    stages: list[StageDefinition] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SopRun(BaseModel):
    id: str
    goal_id: str
    sop_definition_id: str
    sop_version: int
    definition_snapshot: dict[str, Any] = Field(default_factory=dict)
    status: SopRunStatus = SopRunStatus.CREATED
    current_stage_id: str | None = None
    current_step_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class StageRun(BaseModel):
    id: str
    sop_run_id: str
    stage_id: str
    status: StepStatus = StepStatus.PENDING


class StepRun(BaseModel):
    id: str
    sop_run_id: str
    step_id: str
    stage_run_id: str | None = None
    status: StepStatus = StepStatus.PENDING
    task_id: str | None = None
    output_artifact_ids: list[str] = Field(default_factory=list)


class AcceptanceCriteria(BaseModel):
    id: str
    description: str
    required: bool = True


class Task(BaseModel):
    id: str
    step_run_id: str
    title: str = ""
    description: str = ""
    role_id: str
    status: TaskStatus = TaskStatus.PENDING
    acceptance_criteria: list[AcceptanceCriteria] = Field(default_factory=list)
    context_package_id: str | None = None
    input_artifact_ids: list[str] = Field(default_factory=list)
    output_artifact_ids: list[str] = Field(default_factory=list)
    related_run_ids: list[str] = Field(default_factory=list)
    retry_of: str | None = None


class SubagentAssignment(BaseModel):
    id: str
    task_id: str
    role_id: str
    agent_instance_id: str
    runtime_id: str
    session_id: str | None = None


class ContextPackage(BaseModel):
    id: str
    task_id: str
    goal_summary: str
    instructions: str
    constraints: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    expected_output_schema: dict[str, Any] = Field(default_factory=dict)
    base_version: int = 1


class Artifact(BaseModel):
    id: str
    task_id: str
    type: ArtifactType
    uri: str | None = None
    content: str | None = None
    sha256: str | None = None
    summary: str | None = None
    created_at: datetime = Field(default_factory=_now)
    accepted: bool = False


class ValidationResult(BaseModel):
    id: str
    task_id: str
    artifact_id: str
    status: ValidationStatus = ValidationStatus.PENDING
    validator_id: str
    messages: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=_now)


class Review(BaseModel):
    id: str
    task_id: str
    artifact_id: str
    reviewer_role_id: str
    status: ReviewStatus = ReviewStatus.PENDING
    feedback: str | None = None
    created_at: datetime = Field(default_factory=_now)


class Handoff(BaseModel):
    id: str
    from_task_id: str
    to_step_id: str
    artifact_ids: list[str] = Field(default_factory=list)
    context_package_id: str | None = None
    brief: str = ""
    status: HandoffStatus = HandoffStatus.DRAFT
    accepted_at: datetime | None = None


class EventRecord(BaseModel):
    id: str
    stream_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=_now)
