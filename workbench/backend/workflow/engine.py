"""Deterministic SOP graph execution and artifact handoff gates."""

from __future__ import annotations

import json
import uuid
from abc import ABC, abstractmethod
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..artifacts.store import FileArtifactStore
from ..domain.models import (
    Artifact,
    ArtifactType,
    Attempt,
    ContextPackage,
    Goal,
    Handoff,
    HandoffMessageType,
    HandoffStatus,
    Review,
    ReviewStatus,
    SopDefinition,
    SopRun,
    SopRunStatus,
    StepDefinition,
    StepRun,
    StepStatus,
    SubagentAssignment,
    Task,
    TaskStatus,
    ValidationResult,
    ValidationStatus,
)
from .role_contract import (
    AgentRole,
    ReviewVerdict,
    RouteTarget,
    apply_status,
    resolve_route,
)


class WorkflowEngineError(RuntimeError):
    """Raised when an invalid SOP transition is requested."""


class SubagentRunner(ABC):
    """Runtime-neutral execution contract for one task attempt."""

    @abstractmethod
    async def execute(
        self,
        *,
        task: Task,
        assignment: SubagentAssignment,
        context: ContextPackage,
    ) -> Artifact:
        """Execute one bounded task and return its proposed artifact."""


class AcceptanceValidator(ABC):
    """Validator contract for machine-checkable task acceptance."""

    @abstractmethod
    async def validate(self, *, task: Task, artifact: Artifact) -> ValidationResult:
        """Return a structured acceptance decision for an artifact."""


@dataclass(frozen=True)
class ReviewRouteDecision:
    """Engine-owned result of applying one Codex review outcome.

    ``decide_review`` keeps its historical tuple return shape for callers that
    only need the step and route.  New orchestration code can use this richer
    record to consume the Engine-authored handoff and audit target metadata.
    """

    step_run: StepRun
    route: RouteTarget
    review_id: str
    target_step_id: str | None = None
    handoff_id: str | None = None
    rework_count: int = 0


class WorkflowEngine:
    """Own SOP state transitions; never delegates scheduling decisions to agents."""

    def _set_status(self, entity: object, kind: str, value: object) -> None:
        """Write one SOP status field through the role contract guard.

        The Engine is the only actor allowed to mutate SOP state; routing every
        write through :func:`apply_status` keeps that invariant enforceable at
        runtime instead of relying on review discipline.
        """
        apply_status(entity, kind=kind, value=value, actor=AgentRole.SOP_ENGINE)

    def __init__(
        self,
        *,
        store: Any,
        runner: SubagentRunner | None = None,
        runners: dict[str, Any] | None = None,
        validator: AcceptanceValidator,
        artifact_store: FileArtifactStore | None = None,
        max_rework_attempts: int = 3,
    ) -> None:
        if max_rework_attempts < 1:
            raise ValueError("max_rework_attempts must be at least 1")
        if runner is None and not runners:
            raise ValueError("must provide either 'runner' or 'runners'")
        self.store = store
        self.runner = runner
        self._runners: dict[str, Any] = dict(runners or {})
        self.validator = validator
        self.artifact_store = artifact_store
        self.max_rework_attempts = max_rework_attempts
        self._sops: dict[str, SopDefinition] = {}
        self._goals: dict[str, Goal] = {}
        self._handoffs: dict[str, Handoff] = {}

    def _current_attempt_for_task(self, task_id: str) -> Attempt | None:
        """Latest Attempt for a task; None for legacy schema-v1 records."""
        try:
            attempts = [
                Attempt.model_validate(item)
                for item in self.store.list_entities("attempts")
                if item.get("task_id") == task_id
            ]
        except (KeyError, AttributeError):
            return None
        if not attempts:
            return None
        return max(attempts, key=lambda attempt: attempt.sequence)

    def _ensure_attempt_for_task(self, task: Task) -> Attempt:
        """Return the current Attempt, creating a synthetic attempt #1 for
        legacy snapshots that predate schema v2 (execute/read path only)."""
        existing = self._current_attempt_for_task(task.id)
        if existing is not None:
            return existing
        attempt = Attempt(
            id=f"attempt_{task.id}_1",
            task_id=task.id,
            sequence=1,
            status=task.status,
        )
        self.store.save_entity("attempts", attempt)
        return attempt

    def _create_attempt_for_task(self, task: Task) -> Attempt:
        """Create the first Attempt for a newly created Task (schema v2 rule:
        every new task execution MUST have an Attempt record)."""
        attempt = Attempt(
            id=f"attempt_{task.id}_1",
            task_id=task.id,
            sequence=1,
            status=TaskStatus.PENDING,
        )
        self.store.save_entity("attempts", attempt)
        return attempt

    def _resolve_reviewed_attempt_id(
        self, *, reviewed_task_id: str | None, reviewed_artifact_id: str | None
    ) -> str | None:
        """Bind a Review to the exact Attempt that produced the reviewed
        artifact.  Falls back to the reviewed task's current Attempt; legacy
        schema-v1 evidence has no attempt linkage (None)."""
        if reviewed_artifact_id:
            try:
                reviewed_artifact = self._get_model(
                    "artifacts", reviewed_artifact_id, Artifact
                )
            except (KeyError, TypeError, ValueError):
                reviewed_artifact = None
            if reviewed_artifact is not None and reviewed_artifact.attempt_id:
                return reviewed_artifact.attempt_id
        if reviewed_task_id:
            attempt = self._current_attempt_for_task(reviewed_task_id)
            if attempt is not None:
                return attempt.id
        return None

    def _ensure_reviewed_attempt_id(
        self, *, reviewed_task_id: str | None, reviewed_artifact_id: str | None
    ) -> str | None:
        """Like _resolve_reviewed_attempt_id but guarantees a value for
        every NEW Review: when the reviewed evidence is legacy (no Attempt
        record), materialize a synthetic attempt #1 for the reviewed task on
        the legacy read path so the Review never carries None."""
        attempt_id = self._resolve_reviewed_attempt_id(
            reviewed_task_id=reviewed_task_id,
            reviewed_artifact_id=reviewed_artifact_id,
        )
        if attempt_id is not None:
            return attempt_id
        if reviewed_task_id:
            try:
                reviewed_task = self._get_model("tasks", reviewed_task_id, Task)
            except (KeyError, TypeError, ValueError):
                return None
            attempt = self._ensure_attempt_for_task(reviewed_task)
            return attempt.id
        return None

    def _adapter_for(self, assignment: SubagentAssignment) -> Any | None:
        """Resolve the ExecutionAdapter for an assignment (contract v2).

        Resolution order: assignment.runtime_id -> runners dict; then
        RoleBinding (role_id -> runtime_id) -> runners dict; else None
        (legacy single-runner path).
        """
        if not self._runners:
            return None
        if assignment.runtime_id in self._runners:
            return self._runners[assignment.runtime_id]
        try:
            bindings = [
                item
                for item in self.store.list_entities("role_bindings")
                if item.get("role_id") == assignment.role_id
            ]
        except (KeyError, AttributeError):
            bindings = []
        for binding in bindings:
            runtime_id = binding.get("runtime_id")
            if runtime_id in self._runners:
                return self._runners[runtime_id]
        return None

    def start_sop_run(self, *, goal: Goal, sop: SopDefinition) -> SopRun:
        steps = self._flatten_steps(sop)
        self._validate_graph(steps)
        run = SopRun(
            id=str(uuid.uuid4()),
            goal_id=goal.id,
            sop_definition_id=sop.id,
            sop_version=sop.version,
            definition_snapshot=sop.model_dump(mode="json"),
            status=SopRunStatus.RUNNING,
            started_at=goal.created_at,
        )
        self._sops[run.id] = sop
        self._goals[goal.id] = goal
        self.store.save_entity("goals", goal)
        self.store.save_entity("sop_runs", run)
        for stage in sop.stages:
            self.store.save_entity(
                "stage_runs",
                {
                    "id": f"{run.id}:{stage.id}",
                    "sop_run_id": run.id,
                    "stage_id": stage.id,
                    "status": StepStatus.PENDING.value,
                },
            )
        for step in steps.values():
            status = StepStatus.READY if not step.depends_on else StepStatus.PENDING
            step_run = StepRun(
                id=f"{run.id}:{step.id}",
                sop_run_id=run.id,
                step_id=step.id,
                stage_run_id=f"{run.id}:{self._stage_for_step(sop, step.id)}",
                status=status,
            )
            self.store.save_entity("step_runs", step_run)
        self._emit(run.id, "sop_started", {"goal_id": goal.id, "sop_id": sop.id})
        return run

    def ready_step_runs(self, sop_run_id: str) -> list[StepRun]:
        return [
            StepRun.model_validate(item)
            for item in self.store.list_entities("step_runs")
            if item.get("sop_run_id") == sop_run_id
            and item.get("status") == StepStatus.READY.value
        ]

    def step_definition(self, sop_run_id: str, step_id: str) -> StepDefinition:
        sop = self._require_sop(sop_run_id)
        steps = self._flatten_steps(sop)
        try:
            return steps[step_id]
        except KeyError as exc:
            raise WorkflowEngineError(f"unknown SOP step: {step_id}") from exc

    def create_task(
        self,
        step_run_id: str,
        *,
        role_id: str,
        title: str = "",
        description: str = "",
    ) -> Task:
        step_run = self._get_model("step_runs", step_run_id, StepRun)
        if step_run.status is not StepStatus.READY:
            raise WorkflowEngineError("task can only be created for a ready step")
        step = self.step_definition(step_run.sop_run_id, step_run.step_id)
        if role_id != step.role_id:
            raise WorkflowEngineError("task role does not match SOP step role")
        previous_task_id = step_run.task_id
        previous_task: Task | None = None
        if previous_task_id:
            try:
                previous_task = self._get_model("tasks", previous_task_id, Task)
            except KeyError:
                # Old snapshots may retain a task id without the task record;
                # creating a fresh attempt remains safe in that case.
                previous_task = None
        task = Task(
            id=str(uuid.uuid4()),
            step_run_id=step_run.id,
            title=title or step.name,
            description=description or step.instructions,
            role_id=role_id,
            acceptance_criteria=list(step.acceptance_criteria),
            retry_of=previous_task.id if previous_task is not None else None,
        )
        step_run.task_id = task.id
        self.store.save_entity("tasks", task)
        attempt = self._create_attempt_for_task(task)
        if previous_task is not None:
            previous_attempt = self._ensure_attempt_for_task(previous_task)
            attempt.previous_attempt_id = previous_attempt.id
            self.store.save_entity("attempts", attempt)
        self.store.save_entity("step_runs", step_run)
        self._emit(
            step_run.sop_run_id,
            "step_started",
            {"step_id": step.id, "task_id": task.id},
        )
        self._emit(
            step_run.sop_run_id,
            "task_created",
            {"task_id": task.id, "step_id": step_run.step_id},
        )
        self._emit(
            step_run.sop_run_id,
            "attempt_created",
            {"attempt_id": attempt.id, "task_id": task.id, "sequence": attempt.sequence},
        )
        return task

    async def execute_task(
        self,
        task_id: str,
        *,
        assignment: SubagentAssignment,
        context: ContextPackage,
    ) -> ExecutionResult:
        task = self._get_model("tasks", task_id, Task)
        step_run = self._get_model("step_runs", task.step_run_id, StepRun)
        step = self.step_definition(step_run.sop_run_id, step_run.step_id)
        if assignment.task_id != task.id or assignment.role_id != task.role_id:
            raise WorkflowEngineError("assignment does not match task")
        if context.task_id != task.id:
            raise WorkflowEngineError("context package does not match task")
        if task.status is not TaskStatus.PENDING:
            raise WorkflowEngineError(
                f"task {task.id} is not pending (status={task.status.value}); "
                "duplicate execute blocked"
            )
        self._require_run_active(step_run.sop_run_id)

        self._set_status(task, "tasks", TaskStatus.RUNNING)
        task.context_package_id = context.id
        task.input_artifact_ids = list(context.artifact_ids)
        self._set_status(step_run, "step_runs", StepStatus.RUNNING)
        attempt = self._ensure_attempt_for_task(task)
        self._set_status(attempt, "attempts", TaskStatus.RUNNING)
        attempt.started_at = datetime.now(UTC)
        attempt.session_id = assignment.session_id
        attempt.runtime_id = assignment.runtime_id
        attempt.agent_instance_id = assignment.agent_instance_id
        self.store.save_entity("attempts", attempt)
        self.store.save_entity("tasks", task)
        self.store.save_entity("step_runs", step_run)
        self.store.save_entity("contexts", context)
        self.store.save_entity("assignments", assignment)
        self._emit(
            step_run.sop_run_id,
            "task_started",
            {"task_id": task.id, "attempt": attempt.sequence},
        )

        adapter = self._adapter_for(assignment)
        try:
            if adapter is not None:
                result = await adapter.execute(
                    task=task, assignment=assignment, context=context
                )
                if any(item is None for item in result.artifacts):
                    from loguru import logger as _lg

                    _lg.error(
                        "runner {} returned None artifact: {}",
                        type(adapter).__name__,
                        result.artifacts,
                    )
                artifacts = [
                    item.model_copy(
                        update={
                            "task_id": task.id,
                            "producer_step_run_id": step_run.id,
                            "attempt_id": attempt.id,
                            "schema_version": 2,
                            "accepted": False,
                        }
                    )
                    for item in result.artifacts
                ]
                if result.session_id:
                    attempt.session_id = result.session_id
                    self.store.save_entity("attempts", attempt)
            else:
                if self.runner is None:
                    raise WorkflowEngineError(
                        "no runner configured for runtime "
                        f"{assignment.runtime_id!r}"
                    )
                single = await self.runner.execute(
                    task=task, assignment=assignment, context=context
                )
                artifacts = [
                    single.model_copy(
                        update={
                            "task_id": task.id,
                            "producer_step_run_id": step_run.id,
                            "attempt_id": attempt.id,
                            "schema_version": 2,
                            "accepted": False,
                        }
                    )
                ]
        except Exception:
            try:
                self._require_run_active(step_run.sop_run_id)
            except WorkflowEngineError:
                pass  # cancelled/paused during execution: states already invalidated
            else:
                self._fail_task_via_engine(task, reason="runner exception")
            raise
        self._require_run_active(step_run.sop_run_id)
        if self.artifact_store is not None:
            stored: list[Artifact] = []
            for item in artifacts:
                content = item.content or item.summary or ""
                stored.append(self.artifact_store.put(item, content))
            artifacts = stored
        for item in artifacts:
            self.store.save_entity("artifacts", item)
        task.output_artifact_ids = [item.id for item in artifacts]
        for item in artifacts:
            if item.id not in step_run.output_artifact_ids:
                step_run.output_artifact_ids.append(item.id)
        artifact = artifacts[0]
        self._set_status(task, "tasks", TaskStatus.VALIDATING)
        self._set_status(step_run, "step_runs", StepStatus.VALIDATING)
        self.store.save_entity("tasks", task)
        self.store.save_entity("step_runs", step_run)
        self._emit(
            step_run.sop_run_id,
            "artifact_created",
            {"artifact_id": artifact.id, "task_id": task.id},
        )

        try:
            validation = await self.validator.validate(task=task, artifact=artifact)
        except Exception:
            try:
                self._require_run_active(step_run.sop_run_id)
            except WorkflowEngineError:
                pass  # cancelled/paused during validation: states already invalidated
            else:
                self._fail_task_via_engine(task, reason="validator exception")
            raise
        self._require_run_active(step_run.sop_run_id)
        self.store.save_entity("validations", validation)
        validation_event = (
            "validation_completed"
            if validation.status is ValidationStatus.ACCEPTED
            else "validation_rejected"
        )
        self._emit(
            step_run.sop_run_id,
            validation_event,
            {
                "task_id": task.id,
                "artifact_id": artifact.id,
                "status": validation.status.value,
            },
        )
        if validation.status is not ValidationStatus.ACCEPTED:
            self._set_status(task, "tasks", TaskStatus.REJECTED)
            self._set_status(step_run, "step_runs", StepStatus.REJECTED)
            self._set_status(attempt, "attempts", TaskStatus.REJECTED)
            attempt.completed_at = datetime.now(UTC)
            self.store.save_entity("attempts", attempt)
            self.store.save_entity("tasks", task)
            self.store.save_entity("step_runs", step_run)
            self._emit(
                step_run.sop_run_id,
                "task_rejected",
                {"task_id": task.id, "messages": validation.messages},
            )
            return ExecutionResult(task=task, artifact=artifact, validation=validation)

        for accepted_item in artifacts:
            accepted_item.accepted = True
            self.store.save_entity("artifacts", accepted_item)
        handoff: Handoff | None = None
        if step.requires_review and artifact.type is not ArtifactType.REVIEW_REPORT:
            self._set_status(task, "tasks", TaskStatus.ACCEPTED)
            self._set_status(step_run, "step_runs", StepStatus.WAITING_REVIEW)
            review = Review(
                id=str(uuid.uuid4()),
                task_id=task.id,
                artifact_id=artifact.id,
                reviewer_role_id=step.role_id,
                reviewed_task_id=task.id,
                reviewed_artifact_id=artifact.id,
                reviewed_attempt_id=attempt.id,
            )
            self.store.save_entity("reviews", review)
            self._emit(
                step_run.sop_run_id,
                "review_requested",
                {"review_id": review.id, "task_id": task.id},
            )
        elif artifact.type is ArtifactType.REVIEW_REPORT:
            # A review report is itself a gate.  Keep the reviewer step waiting
            # until the Engine applies PASS/REWORK/PLAN_INVALID.
            self._set_status(task, "tasks", TaskStatus.ACCEPTED)
            self._set_status(step_run, "step_runs", StepStatus.WAITING_REVIEW)
            self.ensure_review_for_report(
                task=task,
                artifact=artifact,
                context=context,
            )
        else:
            self._set_status(task, "tasks", TaskStatus.ACCEPTED)
            self._set_status(step_run, "step_runs", StepStatus.COMPLETED)
            self._emit(step_run.sop_run_id, "task_accepted", {"task_id": task.id})
            handoff = self._complete_step_and_create_handoff(step_run, task, step)
        self._set_status(attempt, "attempts", TaskStatus.ACCEPTED)
        attempt.completed_at = datetime.now(UTC)
        self.store.save_entity("attempts", attempt)
        self.store.save_entity("tasks", task)
        self.store.save_entity("step_runs", step_run)
        self._refresh_run_status(step_run.sop_run_id)
        return ExecutionResult(
            task=task,
            artifact=artifact,
            validation=validation,
            handoff=handoff,
        )

    def accept_handoff(self, handoff_id: str) -> StepRun:
        handoff = self._get_model("handoffs", handoff_id, Handoff)
        if handoff.status is not HandoffStatus.READY:
            raise WorkflowEngineError("handoff is not ready for acceptance")
        task = self._get_model("tasks", handoff.from_task_id, Task)
        source_step = self._get_model("step_runs", task.step_run_id, StepRun)
        target = self._find_target_step_run(source_step.sop_run_id, handoff.to_step_id)
        if not self._dependencies_satisfied(target, candidate_handoff_id=handoff.id):
            raise WorkflowEngineError(
                "handoff accepted before all dependencies were satisfied"
            )
        self._set_status(handoff, "handoffs", HandoffStatus.ACCEPTED)
        handoff.accepted_at = __import__("datetime").datetime.now(
            __import__("datetime").UTC
        )
        self.store.save_entity("handoffs", handoff)
        self._set_status(target, "step_runs", StepStatus.READY)
        self.store.save_entity("step_runs", target)
        self._emit(
            source_step.sop_run_id,
            "handoff_accepted",
            {"handoff_id": handoff.id, "to_step_id": target.step_id},
        )
        self._emit(source_step.sop_run_id, "step_ready", {"step_id": target.step_id})
        self._refresh_run_status(source_step.sop_run_id)
        return target

    def _finalize_reviewer_step(
        self, review: Review, *, create_handoff: bool
    ) -> None:
        """Close a dedicated reviewer step after its outcome is applied."""
        reviewer_task = self._get_model("tasks", review.task_id, Task)
        reviewed_task = self._reviewed_task(review)
        if reviewer_task.id == reviewed_task.id:
            return
        reviewer_step_run = self._get_model(
            "step_runs", reviewer_task.step_run_id, StepRun
        )
        if reviewer_step_run.status is StepStatus.COMPLETED:
            return
        reviewer_step = self.step_definition(
            reviewer_step_run.sop_run_id, reviewer_step_run.step_id
        )
        self._set_status(reviewer_task, "tasks", TaskStatus.ACCEPTED)
        self._set_status(reviewer_step_run, "step_runs", StepStatus.COMPLETED)
        self.store.save_entity("tasks", reviewer_task)
        self.store.save_entity("step_runs", reviewer_step_run)
        if create_handoff:
            self._complete_step_and_create_handoff(
                reviewer_step_run, reviewer_task, reviewer_step
            )

    def approve_review(self, review_id: str) -> StepRun:
        review = self._get_model("reviews", review_id, Review)
        if review.status is not ReviewStatus.PENDING:
            raise WorkflowEngineError("review is already decided")
        self._set_status(review, "reviews", ReviewStatus.APPROVED)
        self.store.save_entity("reviews", review)
        task = self._reviewed_task(review)
        step_run = self._get_model("step_runs", task.step_run_id, StepRun)
        step = self.step_definition(step_run.sop_run_id, step_run.step_id)
        self._set_status(task, "tasks", TaskStatus.ACCEPTED)
        was_completed = step_run.status is StepStatus.COMPLETED
        if not was_completed:
            self._set_status(step_run, "step_runs", StepStatus.COMPLETED)
        self.store.save_entity("tasks", task)
        self.store.save_entity("step_runs", step_run)
        if not was_completed:
            self._complete_step_and_create_handoff(step_run, task, step)
        else:
            self._unlock_dependents(step_run.sop_run_id, step.id)
        with suppress(KeyError, TypeError, ValueError, WorkflowEngineError):
            self._finalize_reviewer_step(review, create_handoff=True)
        self._emit(step_run.sop_run_id, "review_approved", {"review_id": review.id})
        self._refresh_run_status(step_run.sop_run_id)
        return step_run

    def reject_review(self, review_id: str, *, feedback: str) -> StepRun:
        review = self._get_model("reviews", review_id, Review)
        if review.status is not ReviewStatus.PENDING:
            raise WorkflowEngineError("review is already decided")
        self._set_status(review, "reviews", ReviewStatus.CHANGES_REQUESTED)
        review.feedback = feedback.strip()
        self.store.save_entity("reviews", review)
        task = self._reviewed_task(review)
        step_run = self._get_model("step_runs", task.step_run_id, StepRun)
        self._set_status(task, "tasks", TaskStatus.REWORK)
        self._set_status(
            step_run,
            "step_runs",
            StepStatus.REWORK if hasattr(StepStatus, "REWORK") else StepStatus.READY,
        )
        self.store.save_entity("tasks", task)
        self.store.save_entity("step_runs", step_run)
        with suppress(KeyError, TypeError, ValueError, WorkflowEngineError):
            self._finalize_reviewer_step(review, create_handoff=False)
        self._emit(
            step_run.sop_run_id,
            "review_rejected",
            {
                "review_id": review.id,
                "task_id": task.id,
                "feedback": review.feedback,
            },
        )
        self._refresh_run_status(step_run.sop_run_id)
        return step_run

    def _extract_review_feedback(self, review: Review) -> str:
        """Extract structured feedback from the review's artifact.

        Returns the blocking items as formatted feedback for the executor.
        Falls back to routing metadata if artifact parsing fails.
        """
        try:
            artifact = self._get_model("artifacts", review.artifact_id, Artifact)
            data = json.loads(artifact.content or "")
            if isinstance(data, dict):
                blocking = data.get("blocking", [])
                if isinstance(blocking, list) and blocking:
                    items = "\n".join(f"- {item}" for item in blocking)
                    return f"Review feedback - blocking issues:\n{items}"
                result = data.get("result", data.get("verdict"))
                if isinstance(result, str) and result.strip():
                    return result.strip()
        except (KeyError, OSError, TypeError, ValueError):
            pass
        return review.feedback or "Review requires changes"

    def _evidence_gate(self, review: Review) -> str | None:
        """Return None when a PASS review's evidence is valid, else an error.

        Only REVIEW_REPORT-based reviews are gateable; legacy review-request
        reviews (created against the executor artifact before the reviewer
        produced a report) bypass the gate - their reviewer report flow is
        gated when it arrives via ensure_review_for_report.
        """
        try:
            artifact = self._get_model("artifacts", review.artifact_id, Artifact)
        except (KeyError, TypeError, ValueError):
            return f"review artifact {review.artifact_id} is missing"
        if artifact.type is not ArtifactType.REVIEW_REPORT:
            return None
        # v2 pipeline:PLAN 类评审(方案审核/复审/最终门)天然没有 DIFF/TEST
        # 执行证据,评审报告自身的 blocking/non_blocking 就是记录 —— 豁免
        # 证据门;执行类评审(final_decision 审 output)仍走完整证据门。
        try:
            reviewed_task = self._reviewed_task(review)
            step_run = self._get_model("step_runs", reviewed_task.step_run_id, StepRun)
            step = self.step_definition(step_run.sop_run_id, step_run.step_id)
            if step.output_type is ArtifactType.PLAN:
                return None
        except (KeyError, TypeError, ValueError, WorkflowEngineError):
            pass  # 结构未知 → 走严格证据门
        from .lineage import LineageError, validate_review_evidence

        try:
            validate_review_evidence(review=review, store=self.store)
        except LineageError as exc:
            return str(exc)
        return None

    def _reviewed_task(self, review: Review) -> Task:
        """Resolve the execution task a review is judging.

        New review records carry an explicit link. For legacy records, infer
        the target from an artifact handed into the reviewer task.
        """
        if review.reviewed_task_id:
            try:
                return self._get_model("tasks", review.reviewed_task_id, Task)
            except KeyError:
                pass

        reviewer_task = self._get_model("tasks", review.task_id, Task)
        artifact_ids = list(reviewer_task.input_artifact_ids)
        if not artifact_ids and reviewer_task.context_package_id:
            try:
                context = self._get_model(
                    "contexts", reviewer_task.context_package_id, ContextPackage
                )
            except KeyError:
                context = None
            if context is not None:
                artifact_ids = list(context.artifact_ids)
        candidate_artifacts: list[Artifact] = []
        for artifact_id in artifact_ids:
            try:
                artifact = self._get_model("artifacts", artifact_id, Artifact)
                if artifact.task_id != reviewer_task.id:
                    candidate_artifacts.append(artifact)
            except KeyError:
                continue
        priority = {
            ArtifactType.IMPLEMENTATION: 0,
            ArtifactType.DIFF: 1,
            ArtifactType.TEST_REPORT: 2,
            ArtifactType.PLAN: 3,
            ArtifactType.REVIEW_REPORT: 4,
        }
        for artifact in sorted(
            candidate_artifacts,
            key=lambda item: priority.get(item.type, 5),
        ):
            try:
                return self._get_model("tasks", artifact.task_id, Task)
            except KeyError:
                continue

        # A caller may provide a minimal ContextPackage that omits artifact
        # ids.  For a dedicated review step, the SOP dependency graph still
        # identifies the execution attempt being reviewed.
        try:
            reviewer_step_run = self._get_model(
                "step_runs", reviewer_task.step_run_id, StepRun
            )
            reviewer_step = self.step_definition(
                reviewer_step_run.sop_run_id, reviewer_step_run.step_id
            )
            for dependency in reversed(reviewer_step.depends_on):
                dependency_run = self._find_target_step_run(
                    reviewer_step_run.sop_run_id, dependency
                )
                if dependency_run.task_id:
                    return self._get_model("tasks", dependency_run.task_id, Task)
        except (KeyError, TypeError, ValueError, WorkflowEngineError):
            pass
        return reviewer_task

    def ensure_review_for_report(
        self,
        *,
        task: Task,
        artifact: Artifact,
        context: ContextPackage,
    ) -> Review:
        """Persist a Review when a Codex task emits a structured report."""
        for item in self.store.list_entities("reviews"):
            existing = Review.model_validate(item)
            if existing.artifact_id == artifact.id:
                return existing

        reviewed_task_id: str | None = None
        reviewed_artifact_id: str | None = None
        review_request_handoff_id: str | None = None
        correlation_id: str | None = None
        input_artifacts: list[Artifact] = []
        for artifact_id in context.artifact_ids:
            try:
                input_artifact = self._get_model("artifacts", artifact_id, Artifact)
                if input_artifact.task_id != task.id:
                    input_artifacts.append(input_artifact)
            except KeyError:
                continue
        # Bind the review to the CURRENT attempt: group input artifacts by
        # task, pick the newest task (by its newest artifact), then the
        # highest-priority artifact type within it.  Never guess the reviewed
        # object from historical artifacts.
        by_task: dict[str, list[Artifact]] = {}
        for item in input_artifacts:
            by_task.setdefault(item.task_id, []).append(item)
        if by_task:
            newest_task_id = max(
                by_task,
                key=lambda tid: max(
                    (a.created_at for a in by_task[tid]), default=datetime.min
                ),
            )
            for input_artifact in sorted(
                by_task[newest_task_id],
                key=lambda item: {
                    ArtifactType.IMPLEMENTATION: 0,
                    ArtifactType.DIFF: 1,
                    ArtifactType.TEST_REPORT: 2,
                    ArtifactType.PLAN: 3,
                    ArtifactType.REVIEW_REPORT: 4,
                }.get(item.type, 5),
            ):
                reviewed_task_id = input_artifact.task_id
                reviewed_artifact_id = input_artifact.id
                break

        # Preserve the request/reply chain when the reviewer was reached via a
        # Dispatcher handoff.  Match only accepted handoffs for this run and
        # this task's input artifacts.
        task_step_run = self._get_model("step_runs", task.step_run_id, StepRun)
        try:
            handoffs = self.store.list_entities("handoffs")
        except (KeyError, AttributeError):
            handoffs = []
        for item in handoffs:
            if item.get("to_step_id") != task_step_run.step_id or item.get(
                "status"
            ) != HandoffStatus.ACCEPTED.value:
                continue
            if set(item.get("artifact_ids", [])) & set(context.artifact_ids):
                review_request_handoff_id = item.get("id")
                correlation_id = item.get("correlation_id")

        if review_request_handoff_id is None:
            # Minimal builders may omit artifact ids. Recover the latest
            # accepted handoff addressed to this review step in the same run.
            for item in handoffs:
                if item.get("to_step_id") != task_step_run.step_id or item.get(
                    "status"
                ) != HandoffStatus.ACCEPTED.value:
                    continue
                try:
                    source_task = self._get_model(
                        "tasks", item["from_task_id"], Task
                    )
                    source_step = self._get_model(
                        "step_runs", source_task.step_run_id, StepRun
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if source_step.sop_run_id == task_step_run.sop_run_id:
                    review_request_handoff_id = item.get("id")
                    correlation_id = item.get("correlation_id")
        if reviewed_task_id is None:
            inferred = self._reviewed_task(
                Review(
                    id="_pending",
                    task_id=task.id,
                    artifact_id=artifact.id,
                    reviewer_role_id=task.role_id,
                )
            )
            if inferred.id != task.id:
                reviewed_task_id = inferred.id
                if inferred.output_artifact_ids:
                    reviewed_artifact_id = inferred.output_artifact_ids[-1]

        reviewed_attempt_id = self._ensure_reviewed_attempt_id(
            reviewed_task_id=reviewed_task_id,
            reviewed_artifact_id=reviewed_artifact_id,
        )
        reviewed_artifacts = [
            item
            for item in input_artifacts
            if reviewed_task_id is not None and item.task_id == reviewed_task_id
        ]
        reviewed_artifact_ids = [item.id for item in reviewed_artifacts]
        evidence_ids = [
            item.id
            for item in reviewed_artifacts
            if item.type in {ArtifactType.DIFF, ArtifactType.TEST_REPORT}
        ]

        review = Review(
            id=str(uuid.uuid4()),
            task_id=task.id,
            artifact_id=artifact.id,
            reviewer_role_id=task.role_id,
            reviewed_task_id=reviewed_task_id,
            reviewed_artifact_id=reviewed_artifact_id,
            reviewed_attempt_id=reviewed_attempt_id,
            reviewed_artifact_ids=reviewed_artifact_ids,
            evidence_ids=evidence_ids,
            review_request_handoff_id=review_request_handoff_id,
            correlation_id=correlation_id,
        )
        self.store.save_entity("reviews", review)
        self._emit(
            self._get_model("step_runs", task.step_run_id, StepRun).sop_run_id,
            "review_requested",
            {
                "review_id": review.id,
                "task_id": task.id,
                "reviewed_task_id": reviewed_task_id,
            },
        )
        return review

    # Compatibility alias for early routing prototypes.
    _ensure_review_for_report = ensure_review_for_report

    def _review_artifact_data(
        self,
        review: Review,
        *,
        allow_legacy_artifact: bool = False,
    ) -> dict[str, Any] | None:
        """Load a structured Codex report, if the artifact still exists."""
        try:
            artifact = self._get_model("artifacts", review.artifact_id, Artifact)
        except KeyError:
            return None
        if artifact.type is not ArtifactType.REVIEW_REPORT:
            if allow_legacy_artifact:
                return None
            raise WorkflowEngineError(
                f"review artifact {artifact.id} must have type review_report"
            )
        try:
            data = json.loads(artifact.content or "")
        except (TypeError, ValueError) as exc:
            raise WorkflowEngineError(
                f"review artifact {artifact.id} is not valid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise WorkflowEngineError(
                f"review artifact {artifact.id} must contain a JSON object"
            )
        result = data.get("result", data.get("verdict"))
        if not isinstance(result, str) or result.strip().upper() not in {
            "PASS",
            "REWORK",
            "PLAN_INVALID",
        }:
            raise WorkflowEngineError(
                f"review artifact {artifact.id} has an invalid result"
            )
        for field in ("blocking", "non_blocking", "evidence"):
            values = data.get(field, [])
            if not isinstance(values, list) or not all(
                isinstance(value, str) for value in values
            ):
                raise WorkflowEngineError(
                    f"review artifact {artifact.id} field {field!r} must be a list of strings"
                )
        if result.strip().upper() != "PASS" and not data.get("blocking"):
            raise WorkflowEngineError(
                f"review artifact {artifact.id} requires blocking items for {result!r}"
            )
        return data

    @staticmethod
    def _normalise_verdict(value: object) -> str:
        raw = getattr(value, "value", value)
        if not isinstance(raw, str) or not raw.strip():
            raise WorkflowEngineError("review verdict is required")
        return raw.strip().upper()

    def _resolve_plan_target(self, sop_run_id: str, reviewed_step_id: str) -> str:
        """Resolve the planner step for a PLAN_INVALID review."""
        sop = self._require_sop(sop_run_id)
        steps = self._flatten_steps(sop)
        metadata = sop.metadata.get("review_route_targets", {})
        if isinstance(metadata, dict):
            for key in (reviewed_step_id, "PLAN_INVALID", "plan_invalid", "plan"):
                target = metadata.get(key)
                if isinstance(target, str) and target in steps:
                    return target

        ancestors: set[str] = set()

        def collect(step_id: str) -> None:
            for dependency in steps[step_id].depends_on:
                if dependency not in ancestors:
                    ancestors.add(dependency)
                    collect(dependency)

        collect(reviewed_step_id)
        if not ancestors:
            plan_steps = [
                step_id
                for step_id, step in steps.items()
                if step.output_type is ArtifactType.PLAN and step_id != reviewed_step_id
            ]
            if len(plan_steps) == 1:
                return plan_steps[0]
            return reviewed_step_id

        def score(step_id: str) -> tuple[int, int, str]:
            step = steps[step_id]
            text = " ".join((step_id, step.name, step.role_id, step.instructions)).lower()
            output_is_plan = int(step.output_type is ArtifactType.PLAN)
            keyword = int(any(word in text for word in ("plan", "planner", "claude")))
            root = int(not step.depends_on)
            return output_is_plan, keyword + root, step_id

        return max(ancestors, key=score)

    def _route_target_step_id(self, route: RouteTarget, reviewed_task: Task) -> str:
        reviewed_step = self._get_model("step_runs", reviewed_task.step_run_id, StepRun)
        try:
            sop = self._require_sop(reviewed_step.sop_run_id)
            steps = self._flatten_steps(sop)
            metadata = sop.metadata.get("review_route_targets", {})
            if isinstance(metadata, dict):
                keys = (
                    reviewed_step.step_id,
                    route.name,
                    route.value,
                    route.value.upper(),
                )
                for key in keys:
                    target = metadata.get(key)
                    if isinstance(target, str) and target in steps:
                        return target
        except (KeyError, TypeError, ValueError, WorkflowEngineError):
            pass
        if route is RouteTarget.RERUN_EXECUTE:
            return reviewed_step.step_id
        if route is RouteTarget.RETURN_TO_PLAN:
            try:
                return self._resolve_plan_target(
                    reviewed_step.sop_run_id, reviewed_step.step_id
                )
            except (KeyError, TypeError, ValueError, WorkflowEngineError):
                # Legacy snapshots may not contain a reconstructable SOP graph;
                # keep the route fail-safe by retrying the reviewed step.
                return reviewed_step.step_id
        raise WorkflowEngineError(f"route {route.value} has no rework target")

    def _effective_rework_limit(self, step: StepDefinition) -> int:
        # Keep legacy ``retry_limit=0`` definitions safe via the Engine cap.
        return step.retry_limit if step.retry_limit > 0 else self.max_rework_attempts

    def _descendant_step_ids(self, sop_run_id: str, root_step_id: str) -> set[str]:
        steps = self._flatten_steps(self._require_sop(sop_run_id))
        descendants: set[str] = set()
        changed = True
        while changed:
            changed = False
            for step_id, step in steps.items():
                if step_id in descendants:
                    continue
                if root_step_id in step.depends_on or any(
                    dependency in descendants for dependency in step.depends_on
                ):
                    descendants.add(step_id)
                    changed = True
        return descendants

    def _prepare_route_target(self, target: StepRun) -> None:
        affected = {target.step_id} | self._descendant_step_ids(
            target.sop_run_id, target.step_id
        )
        for item in self.store.list_entities("step_runs"):
            if item.get("sop_run_id") != target.sop_run_id:
                continue
            step_id = item.get("step_id")
            if step_id not in affected:
                continue
            step_run = StepRun.model_validate(item)
            desired = StepStatus.READY if step_id == target.step_id else StepStatus.PENDING
            if step_run.status is not desired:
                self._set_status(step_run, "step_runs", desired)
                self.store.save_entity("step_runs", step_run)

    def _create_review_route_handoff(
        self,
        *,
        review: Review,
        target_step_id: str,
        message_type: HandoffMessageType,
        feedback: str,
        correlation_id: str | None,
    ) -> Handoff:
        reviewer_task = self._get_model("tasks", review.task_id, Task)
        source_step = self._get_model("step_runs", reviewer_task.step_run_id, StepRun)
        artifact_ids = [review.artifact_id]
        if review.reviewed_artifact_id and review.reviewed_artifact_id not in artifact_ids:
            artifact_ids.append(review.reviewed_artifact_id)
        handoff = Handoff(
            id=str(uuid.uuid4()),
            from_task_id=reviewer_task.id,
            to_step_id=target_step_id,
            artifact_ids=artifact_ids,
            brief=feedback,
            status=HandoffStatus.READY,
            message_type=message_type,
            reply_to_handoff_id=review.review_request_handoff_id,
            correlation_id=(
                correlation_id
                or review.correlation_id
                or review.review_request_handoff_id
                or review.id
            ),
        )
        self._handoffs[handoff.id] = handoff
        with suppress(KeyError, AttributeError):
            self.store.save_entity("handoffs", handoff)
        self._emit(
            source_step.sop_run_id,
            "handoff_created",
            {
                "handoff_id": handoff.id,
                "message_type": message_type.value,
                "to_step_id": target_step_id,
                "review_id": review.id,
            },
        )
        return handoff

    def apply_review_outcome(
        self,
        review_id: str,
        *,
        verdict: str | None = None,
        correlation_id: str | None = None,
        _allow_legacy_missing_artifact: bool = False,
        _allow_legacy_artifact: bool = False,
    ) -> ReviewRouteDecision:
        """Apply a Codex outcome and stage the Engine-owned next handoff."""
        review = self._get_model("reviews", review_id, Review)
        if review.status is not ReviewStatus.PENDING:
            raise WorkflowEngineError("review is already decided")
        try:
            reviewed_run_step = self._get_model(
                "step_runs", self._reviewed_task(review).step_run_id, StepRun
            )
        except (KeyError, TypeError, ValueError):
            reviewed_run_step = None
        if reviewed_run_step is not None:
            self._require_run_active(reviewed_run_step.sop_run_id)
        data = self._review_artifact_data(
            review, allow_legacy_artifact=_allow_legacy_artifact
        )
        if data is None and not _allow_legacy_missing_artifact and not _allow_legacy_artifact:
            raise WorkflowEngineError(
                f"review artifact {review.artifact_id} is missing or unstructured"
            )
        report_verdict = None if data is None else data.get("result", data.get("verdict"))
        if data is not None and (
            not isinstance(report_verdict, str) or not report_verdict.strip()
        ):
            raise WorkflowEngineError(
                f"review artifact {review.artifact_id} has no result"
            )
        supplied = self._normalise_verdict(verdict) if verdict is not None else None
        normalized = supplied or self._normalise_verdict(report_verdict)
        if report_verdict is not None:
            report_normalized = self._normalise_verdict(report_verdict)
            if normalized != report_normalized:
                raise WorkflowEngineError(
                    f"supplied review verdict {normalized!r} does not match "
                    f"artifact result {report_normalized!r}"
                )
        # Phase 3: preserve the reviewer's own outcome; the Engine policy
        # decision is separate and never rewrites what the reviewer said.
        review.reviewer_output = normalized
        if normalized == ReviewVerdict.PASS.value:
            gate_error = self._evidence_gate(review)
            if gate_error is not None:
                review.policy_decision = "BLOCKED"
                review.feedback = (
                    "Evidence gate failed: " + gate_error
                    + "\n\nReviewer output: " + normalized
                    + "\nOriginal feedback: " + (review.feedback or "(none)")
                )
                self._set_status(review, "reviews", ReviewStatus.POLICY_BLOCKED)
                self.store.save_entity("reviews", review)
                step_run = self._get_model(
                    "step_runs",
                    self._reviewed_task(review).step_run_id,
                    StepRun,
                )
                self._emit(
                    step_run.sop_run_id,
                    "review_policy_blocked",
                    {
                        "review_id": review.id,
                        "reviewer_output": normalized,
                        "policy_decision": review.policy_decision,
                        "reason": gate_error,
                    },
                )
                self._refresh_run_status(step_run.sop_run_id)
                return ReviewRouteDecision(
                    step_run=step_run,
                    route=RouteTarget.HUMAN,
                    review_id=review.id,
                )
        self.store.save_entity("reviews", review)
        route = resolve_route(normalized, actor=AgentRole.SOP_ENGINE)
        feedback = self._extract_review_feedback(review)
        if route is RouteTarget.ADVANCE:
            step_run = self._get_model("step_runs", self._reviewed_task(review).step_run_id, StepRun)
            self._emit(
                step_run.sop_run_id,
                "review_routed",
                {
                    "review_id": review.id,
                    "verdict": normalized,
                    "route": route.value,
                },
            )
            step_run = self.approve_review(review_id)
            approved = self._get_model("reviews", review_id, Review)
            approved.policy_decision = "APPROVED"
            self.store.save_entity("reviews", approved)
            return ReviewRouteDecision(
                step_run=step_run, route=route, review_id=review.id
            )

        reviewed_task = self._reviewed_task(review)
        target_step_id = self._route_target_step_id(route, reviewed_task)
        reviewed_step = self._get_model("step_runs", reviewed_task.step_run_id, StepRun)
        target = self._find_target_step_run(reviewed_step.sop_run_id, target_step_id)
        try:
            target_step = self.step_definition(target.sop_run_id, target.step_id)
        except (KeyError, TypeError, ValueError, WorkflowEngineError):
            target_step = None
        limit = (
            self._effective_rework_limit(target_step)
            if target_step is not None
            else self.max_rework_attempts
        )
        if target.rework_count >= limit:
            self.reject_review(review_id, feedback=feedback)
            self._set_status(target, "step_runs", StepStatus.FAILED)
            self.store.save_entity("step_runs", target)
            run = self._get_model("sop_runs", target.sop_run_id, SopRun)
            self._set_status(run, "sop_runs", SopRunStatus.FAILED)
            self.store.save_entity("sop_runs", run)
            self._emit(
                target.sop_run_id,
                "rework_exhausted",
                {
                    "review_id": review.id,
                    "target_step_id": target.step_id,
                    "rework_count": target.rework_count,
                },
            )
            return ReviewRouteDecision(
                step_run=target,
                route=RouteTarget.FAIL_RUN,
                review_id=review.id,
                target_step_id=target.step_id,
                rework_count=target.rework_count,
            )

        self.reject_review(review_id, feedback=feedback)
        target = self._find_target_step_run(target.sop_run_id, target_step_id)
        if target.task_id and target.task_id != reviewed_task.id:
            with suppress(KeyError, TypeError, ValueError):
                target_task = self._get_model("tasks", target.task_id, Task)
                if target_task.status is not TaskStatus.REWORK:
                    self._set_status(target_task, "tasks", TaskStatus.REWORK)
                    self.store.save_entity("tasks", target_task)
        target.rework_count += 1
        try:
            self._prepare_route_target(target)
        except (KeyError, TypeError, ValueError, WorkflowEngineError):
            # Preserve compatibility with pre-routing snapshots that only have
            # a StepRun record and no valid definition snapshot.
            self._set_status(target, "step_runs", StepStatus.READY)
        self.store.save_entity("step_runs", target)
        message_type = (
            HandoffMessageType.REWORK
            if route is RouteTarget.RERUN_EXECUTE
            else HandoffMessageType.PLAN_INVALID
        )
        handoff = self._create_review_route_handoff(
            review=review,
            target_step_id=target_step_id,
            message_type=message_type,
            feedback=feedback,
            correlation_id=correlation_id,
        )
        self._emit(
            target.sop_run_id,
            "review_routed",
            {
                "review_id": review.id,
                "verdict": normalized,
                "route": route.value,
                "target_step_id": target_step_id,
                "handoff_id": handoff.id,
                "rework_count": target.rework_count,
                "correlation_id": handoff.correlation_id,
            },
        )
        self._refresh_run_status(target.sop_run_id)
        return ReviewRouteDecision(
            step_run=target,
            route=route,
            review_id=review.id,
            target_step_id=target_step_id,
            handoff_id=handoff.id,
            rework_count=target.rework_count,
        )

    def decide_review(
        self, review_id: str, *, verdict: str | None = None
    ) -> tuple[StepRun, RouteTarget]:
        """Backward-compatible tuple wrapper around ``apply_review_outcome``."""
        review = self._get_model("reviews", review_id, Review)
        legacy_step: StepRun | None = None
        try:
            reviewer_task = self._get_model("tasks", review.task_id, Task)
            legacy_step = self._get_model("step_runs", reviewer_task.step_run_id, StepRun)
        except (KeyError, TypeError, ValueError):
            pass
        decision = self.apply_review_outcome(
            review_id,
            verdict=verdict,
            _allow_legacy_missing_artifact=True,
            _allow_legacy_artifact=True,
        )
        return legacy_step or decision.step_run, decision.route

    def retry_task(self, task_id: str) -> Task:
        previous = self._get_model("tasks", task_id, Task)
        step_run = self._get_model("step_runs", previous.step_run_id, StepRun)
        if previous.status not in {
            TaskStatus.REJECTED,
            TaskStatus.FAILED,
            TaskStatus.REWORK,
        }:
            raise WorkflowEngineError("only failed or rejected tasks can be retried")
        step = self.step_definition(step_run.sop_run_id, step_run.step_id)
        if step_run.rework_count >= self._effective_rework_limit(step):
            raise WorkflowEngineError(
                f"retry limit exhausted for step {step_run.step_id}"
            )
        self._set_status(step_run, "step_runs", StepStatus.READY)
        step_run.rework_count += 1
        retry = previous.model_copy(
            update={
                "id": str(uuid.uuid4()),
                "status": TaskStatus.PENDING,
                "retry_of": previous.id,
                "related_run_ids": [],
                "output_artifact_ids": [],
                "context_package_id": None,
            }
        )
        step_run.task_id = retry.id
        self.store.save_entity("step_runs", step_run)
        self.store.save_entity("tasks", retry)
        attempt = self._create_attempt_for_task(retry)
        previous_attempt = self._ensure_attempt_for_task(previous)
        attempt.previous_attempt_id = previous_attempt.id
        self.store.save_entity("attempts", attempt)
        self._emit(
            step_run.sop_run_id,
            "task_retry_ready",
            {"task_id": retry.id, "retry_of": previous.id},
        )
        self._emit(
            step_run.sop_run_id,
            "attempt_created",
            {"attempt_id": attempt.id, "task_id": retry.id, "sequence": attempt.sequence},
        )
        return retry

    def pause_sop_run(self, *, sop_run_id: str, reason: str = "paused") -> SopRun:
        """Pause an active SOP run (all further step execution halts)."""
        run = self._require_run(sop_run_id)
        if run.status in (
            SopRunStatus.CANCELLED,
            SopRunStatus.FAILED,
            SopRunStatus.COMPLETED,
        ):
            raise WorkflowEngineError(
                f"Cannot pause {sop_run_id}: already {run.status.value}"
            )
        self._set_status(run, "sop_runs", SopRunStatus.PAUSED)
        self.store.save_entity("sop_runs", run)
        self._emit(sop_run_id, "sop_paused", {"reason": reason})
        return run

    def resume_sop_run(self, *, sop_run_id: str) -> SopRun:
        """Resume a paused SOP run."""
        run = self._require_run(sop_run_id)
        if run.status is not SopRunStatus.PAUSED:
            raise WorkflowEngineError(f"SOPRun {sop_run_id} is not paused")
        pending_review = any(
            item.get("status") == StepStatus.WAITING_REVIEW.value
            for item in self.store.list_entities("step_runs")
            if item.get("sop_run_id") == sop_run_id
        )
        target = (
            SopRunStatus.WAITING_REVIEW
            if pending_review
            else SopRunStatus.RUNNING
        )
        self._set_status(run, "sop_runs", target)
        self.store.save_entity("sop_runs", run)
        self._emit(sop_run_id, "sop_resumed", {})
        return run

    def cancel_sop_run(self, *, sop_run_id: str, reason: str = "cancelled") -> SopRun:
        """Cancel an SOP run and invalidate in-flight work.

        RUNNING/READY steps become BLOCKED and RUNNING tasks/attempts become
        FAILED so late callbacks cannot flip the cancelled run back to a live
        state (the run-active gate rejects any late write).
        """
        run = self._require_run(sop_run_id)
        if run.status in (
            SopRunStatus.CANCELLED,
            SopRunStatus.FAILED,
            SopRunStatus.COMPLETED,
        ):
            raise WorkflowEngineError(
                f"Cannot cancel {sop_run_id}: already {run.status.value}"
            )
        for item in self.store.list_entities("step_runs"):
            if item.get("sop_run_id") != sop_run_id:
                continue
            if item.get("status") in {
                StepStatus.RUNNING.value,
                StepStatus.READY.value,
            }:
                step_run = StepRun.model_validate(item)
                self._set_status(step_run, "step_runs", StepStatus.BLOCKED)
                self.store.save_entity("step_runs", step_run)
        for item in self.store.list_entities("tasks"):
            if item.get("status") != TaskStatus.RUNNING.value:
                continue
            try:
                task = self._get_model("tasks", item["id"], Task)
                step_run = self._get_model("step_runs", task.step_run_id, StepRun)
            except (KeyError, TypeError, ValueError):
                continue
            if step_run.sop_run_id != sop_run_id:
                continue
            self._set_status(task, "tasks", TaskStatus.FAILED)
            self.store.save_entity("tasks", task)
            attempt = self._current_attempt_for_task(task.id)
            if attempt is not None and attempt.status is TaskStatus.RUNNING:
                self._set_status(attempt, "attempts", TaskStatus.FAILED)
                attempt.completed_at = datetime.now(UTC)
                self.store.save_entity("attempts", attempt)
        self._set_status(run, "sop_runs", SopRunStatus.CANCELLED)
        self.store.save_entity("sop_runs", run)
        self._emit(sop_run_id, "sop_cancelled", {"reason": reason})
        return run

    def recover_orphaned_tasks(self) -> list[str]:
        """Fail tasks stuck RUNNING with no RUNNING attempt (engine-legal)."""
        recovered: list[str] = []
        for item in self.store.list_entities("tasks"):
            if item.get("status") != TaskStatus.RUNNING.value:
                continue
            task = self._get_model("tasks", item["id"], Task)
            running_attempts = [
                a
                for a in self.store.list_entities("attempts")
                if a.get("task_id") == task.id
                and a.get("status") == TaskStatus.RUNNING.value
            ]
            if running_attempts:
                continue
            self._fail_task_via_engine(
                task, reason="orphaned (RUNNING with no RUNNING attempt)"
            )
            recovered.append(task.id)
        return recovered

    def recover_stuck_step_runs(self, *, timeout_seconds: int = 3600) -> list[str]:
        """Fail steps whose RUNNING attempt exceeded the timeout (engine-legal)."""
        recovered: list[str] = []
        cutoff = datetime.now(UTC) - timedelta(seconds=timeout_seconds)
        for item in self.store.list_entities("attempts"):
            if item.get("status") != TaskStatus.RUNNING.value:
                continue
            attempt = Attempt.model_validate(item)
            started = attempt.started_at
            if started is None or started >= cutoff:
                continue
            try:
                task = self._get_model("tasks", attempt.task_id, Task)
            except (KeyError, TypeError, ValueError):
                continue
            if task.status is not TaskStatus.RUNNING:
                continue
            self._fail_task_via_engine(
                task,
                reason=f"stuck attempt {attempt.id} beyond {timeout_seconds}s",
            )
            recovered.append(task.id)
        return recovered

    def _complete_step_and_create_handoff(
        self, step_run: StepRun, task: Task, step: StepDefinition
    ) -> Handoff | None:
        self._emit(step_run.sop_run_id, "step_completed", {"step_id": step.id})
        if step.handoff_to:
            target_step = self.step_definition(step_run.sop_run_id, step.handoff_to)
            message_type = self._handoff_message_type(
                source_role_id=step.role_id,
                target_role_id=target_step.role_id,
            )
            handoff = Handoff(
                id=str(uuid.uuid4()),
                from_task_id=task.id,
                to_step_id=step.handoff_to,
                artifact_ids=list(task.output_artifact_ids),
                context_package_id=task.context_package_id,
                brief=(
                    f"{step.name or step.id} 已完成；请基于附带 Artifact 继续下一阶段。"
                ),
                status=HandoffStatus.READY,
                message_type=message_type,
            )
            self._handoffs[handoff.id] = handoff
            self.store.save_entity("handoffs", handoff)
            self._emit(
                step_run.sop_run_id,
                "handoff_created",
                {
                    "handoff_id": handoff.id,
                    "to_step_id": handoff.to_step_id,
                    "message_type": handoff.message_type.value,
                    "artifact_ids": list(handoff.artifact_ids),
                },
            )
            return handoff
        self._unlock_dependents(step_run.sop_run_id, step.id)
        return None

    @staticmethod
    def _handoff_message_type(
        *, source_role_id: str, target_role_id: str
    ) -> HandoffMessageType:
        """Derive the fixed-pipeline handoff intent from declared SOP roles.

        Generic SOPs retain ``HANDOFF``.  The mapping only names the four
        role transitions already permitted by ``CommunicationPolicy``; it
        never lets a runtime choose its own routing intent.
        """
        source = source_role_id.lower()
        target = target_role_id.lower()
        source_is_claude = source in {"claude", "planner"}
        source_is_dsh = source in {"dsh", "executor"}
        source_is_codex = source in {"codex", "reviewer"}
        target_is_claude = target in {"claude", "planner"}
        target_is_dsh = target in {"dsh", "executor"}
        target_is_codex = target in {"codex", "reviewer"}

        if source_is_claude and target_is_dsh:
            return HandoffMessageType.PLAN_READY
        if source_is_dsh and target_is_codex:
            return HandoffMessageType.REVIEW_REQUEST
        if source_is_codex and target_is_dsh:
            return HandoffMessageType.REWORK
        if source_is_codex and target_is_claude:
            return HandoffMessageType.PLAN_INVALID
        return HandoffMessageType.HANDOFF

    def _unlock_dependents(self, sop_run_id: str, source_step_id: str) -> None:
        sop = self._require_sop(sop_run_id)
        for candidate in self._flatten_steps(sop).values():
            if source_step_id not in candidate.depends_on:
                continue
            target = self._find_target_step_run(sop_run_id, candidate.id)
            if target.status is StepStatus.PENDING and self._dependencies_satisfied(
                target
            ):
                self._set_status(target, "step_runs", StepStatus.READY)
                self.store.save_entity("step_runs", target)
                self._emit(sop_run_id, "step_ready", {"step_id": target.step_id})

    def _dependencies_satisfied(
        self, target: StepRun, *, candidate_handoff_id: str | None = None
    ) -> bool:
        step = self.step_definition(target.sop_run_id, target.step_id)
        for dependency in step.depends_on:
            source = self._find_target_step_run(target.sop_run_id, dependency)
            if source.status is not StepStatus.COMPLETED:
                return False
            source_step = self.step_definition(target.sop_run_id, dependency)
            if source_step.handoff_to == target.step_id:
                handoffs = [
                    Handoff.model_validate(item)
                    for item in self.store.list_entities("handoffs")
                    if item.get("to_step_id") == target.step_id
                    and (
                        item.get("status") == HandoffStatus.ACCEPTED.value
                        or item.get("id") == candidate_handoff_id
                    )
                    and self._task_belongs_to_step(item["from_task_id"], source.id)
                ]
                if not handoffs:
                    return False
        return True

    def _task_belongs_to_step(self, task_id: str, step_run_id: str) -> bool:
        try:
            return self._get_model("tasks", task_id, Task).step_run_id == step_run_id
        except KeyError:
            return False

    def _refresh_run_status(self, sop_run_id: str) -> None:
        run = self._get_model("sop_runs", sop_run_id, SopRun)
        previous_status = run.status
        steps = [
            StepRun.model_validate(item)
            for item in self.store.list_entities("step_runs")
            if item.get("sop_run_id") == sop_run_id
        ]
        if run.status is SopRunStatus.FAILED or any(
            item.status is StepStatus.FAILED for item in steps
        ):
            self._set_status(run, "sop_runs", SopRunStatus.FAILED)
        elif steps and all(item.status is StepStatus.COMPLETED for item in steps):
            self._set_status(run, "sop_runs", SopRunStatus.COMPLETED)
        elif any(item.status is StepStatus.WAITING_REVIEW for item in steps):
            self._set_status(run, "sop_runs", SopRunStatus.WAITING_REVIEW)
        else:
            self._set_status(run, "sop_runs", SopRunStatus.RUNNING)
        self.store.save_entity("sop_runs", run)

        # Emit status change events
        if previous_status != run.status:
            if run.status is SopRunStatus.COMPLETED:
                self._emit(sop_run_id, "sop_completed", {"sop_run_id": sop_run_id})
            elif run.status is SopRunStatus.WAITING_REVIEW:
                self._emit(sop_run_id, "sop_waiting_review", {"sop_run_id": sop_run_id})
            elif run.status is SopRunStatus.FAILED:
                self._emit(sop_run_id, "sop_failed", {"sop_run_id": sop_run_id})

    def _find_target_step_run(self, sop_run_id: str, step_id: str) -> StepRun:
        for item in self.store.list_entities("step_runs"):
            if item.get("sop_run_id") == sop_run_id and item.get("step_id") == step_id:
                return StepRun.model_validate(item)
        raise WorkflowEngineError(f"unknown step run: {step_id}")

    def _require_run(self, sop_run_id: str) -> SopRun:
        """Load a SopRun or raise WorkflowEngineError."""
        try:
            return self._get_model("sop_runs", sop_run_id, SopRun)
        except (KeyError, TypeError, ValueError):
            raise WorkflowEngineError(f"SOPRun {sop_run_id} not found") from None

    def _require_run_active(
        self, sop_run_id: str, *, allow_paused: bool = False
    ) -> SopRun:
        """Reject state writes on a finished/cancelled/failed run.

        This is the late-callback / stale-generation guard: a result that
        arrives after cancel/pause/fail may not flip the run back to a live
        state.
        """
        run = self._require_run(sop_run_id)
        if run.status in (
            SopRunStatus.CANCELLED,
            SopRunStatus.FAILED,
            SopRunStatus.COMPLETED,
        ):
            raise WorkflowEngineError(
                f"SOPRun {sop_run_id} is {run.status.value}; "
                "refusing late state write"
            )
        if run.status is SopRunStatus.PAUSED and not allow_paused:
            raise WorkflowEngineError(f"SOPRun {sop_run_id} is paused")
        return run

    def _fail_task_via_engine(self, task: Task, *, reason: str) -> None:
        """The ONLY legal recovery path: Engine state transitions only."""
        self._set_status(task, "tasks", TaskStatus.FAILED)
        self.store.save_entity("tasks", task)
        try:
            step_run = self._get_model("step_runs", task.step_run_id, StepRun)
        except (KeyError, TypeError, ValueError):
            return
        if step_run.status not in {StepStatus.COMPLETED, StepStatus.FAILED}:
            self._set_status(step_run, "step_runs", StepStatus.FAILED)
            self.store.save_entity("step_runs", step_run)
        attempt = self._current_attempt_for_task(task.id)
        if attempt is not None and attempt.status not in {
            TaskStatus.FAILED,
            TaskStatus.ACCEPTED,
        }:
            self._set_status(attempt, "attempts", TaskStatus.FAILED)
            attempt.completed_at = datetime.now(UTC)
            self.store.save_entity("attempts", attempt)
        self._emit(
            step_run.sop_run_id,
            "task_failed",
            {"task_id": task.id, "reason": reason},
        )

    def _require_sop(self, sop_run_id: str) -> SopDefinition:
        if sop_run_id not in self._sops:
            run = self._get_model("sop_runs", sop_run_id, SopRun)
            self._sops[sop_run_id] = SopDefinition.model_validate(
                run.definition_snapshot
            )
        return self._sops[sop_run_id]

    def _get_model(self, collection: str, entity_id: str, model: Any) -> Any:
        return model.model_validate(self.store.get_entity(collection, entity_id))

    def _emit(self, stream_id: str, event_type: str, payload: dict[str, Any]) -> None:
        try:
            self.store.append_event(
                stream_id=stream_id,
                event_type=event_type,
                payload=payload,
                backend="sop_engine",
            )
        except TypeError:
            self.store.append_event(
                stream_id=stream_id, event_type=event_type, payload=payload
            )

    @staticmethod
    def _flatten_steps(sop: SopDefinition) -> dict[str, StepDefinition]:
        steps: dict[str, StepDefinition] = {}
        for stage in sop.stages:
            for step in stage.steps:
                if step.id in steps:
                    raise WorkflowEngineError(f"duplicate SOP step: {step.id}")
                steps[step.id] = step
        if not steps:
            raise WorkflowEngineError("SOP must contain at least one step")
        return steps

    @staticmethod
    def _stage_for_step(sop: SopDefinition, step_id: str) -> str:
        for stage in sop.stages:
            if any(step.id == step_id for step in stage.steps):
                return stage.id
        raise WorkflowEngineError(f"step is not in a stage: {step_id}")

    @classmethod
    def _validate_graph(cls, steps: dict[str, StepDefinition]) -> None:
        for step in steps.values():
            missing = set(step.depends_on) - steps.keys()
            if missing:
                raise WorkflowEngineError(
                    f"step {step.id} depends on unknown steps: {sorted(missing)}"
                )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise WorkflowEngineError("SOP dependency graph contains a cycle")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in steps[step_id].depends_on:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in steps:
            visit(step_id)


class ExecutionResult:
    def __init__(
        self,
        *,
        task: Task,
        artifact: Artifact,
        validation: ValidationResult,
        handoff: Handoff | None = None,
    ) -> None:
        self.task = task
        self.artifact = artifact
        self.validation = validation
        self.handoff = handoff
