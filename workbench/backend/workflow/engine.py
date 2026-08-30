"""Deterministic SOP graph execution and artifact handoff gates."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Any

from .role_contract import (
    AgentRole,
    RouteTarget,
    apply_status,
    resolve_route,
)

from ..artifacts.store import FileArtifactStore
from ..domain.models import (
    Artifact,
    ContextPackage,
    Goal,
    Handoff,
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
        runner: SubagentRunner,
        validator: AcceptanceValidator,
        artifact_store: FileArtifactStore | None = None,
    ) -> None:
        self.store = store
        self.runner = runner
        self.validator = validator
        self.artifact_store = artifact_store
        self._sops: dict[str, SopDefinition] = {}
        self._goals: dict[str, Goal] = {}
        self._handoffs: dict[str, Handoff] = {}

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
        task = Task(
            id=str(uuid.uuid4()),
            step_run_id=step_run.id,
            title=title or step.name,
            description=description or step.instructions,
            role_id=role_id,
            acceptance_criteria=list(step.acceptance_criteria),
        )
        step_run.task_id = task.id
        self.store.save_entity("tasks", task)
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

        self._set_status(task, "tasks", TaskStatus.RUNNING)
        task.context_package_id = context.id
        task.input_artifact_ids = list(context.artifact_ids)
        self._set_status(step_run, "step_runs", StepStatus.RUNNING)
        self.store.save_entity("tasks", task)
        self.store.save_entity("step_runs", step_run)
        self.store.save_entity("contexts", context)
        self.store.save_entity("assignments", assignment)
        self._emit(
            step_run.sop_run_id, "task_started", {"task_id": task.id, "attempt": 1}
        )

        artifact = await self.runner.execute(
            task=task, assignment=assignment, context=context
        )
        artifact = artifact.model_copy(
            update={
                "task_id": task.id,
                "producer_step_run_id": step_run.id,
                "accepted": False,
            }
        )
        if self.artifact_store is not None:
            content = artifact.content or artifact.summary or ""
            artifact = self.artifact_store.put(artifact, content)
        self.store.save_entity("artifacts", artifact)
        task.output_artifact_ids = [artifact.id]
        step_run.output_artifact_ids = [artifact.id]
        self._set_status(task, "tasks", TaskStatus.VALIDATING)
        self._set_status(step_run, "step_runs", StepStatus.VALIDATING)
        self.store.save_entity("tasks", task)
        self.store.save_entity("step_runs", step_run)
        self._emit(
            step_run.sop_run_id,
            "artifact_created",
            {"artifact_id": artifact.id, "task_id": task.id},
        )

        validation = await self.validator.validate(task=task, artifact=artifact)
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
            self.store.save_entity("tasks", task)
            self.store.save_entity("step_runs", step_run)
            self._emit(
                step_run.sop_run_id,
                "task_rejected",
                {"task_id": task.id, "messages": validation.messages},
            )
            return ExecutionResult(task=task, artifact=artifact, validation=validation)

        artifact.accepted = True
        self.store.save_entity("artifacts", artifact)
        handoff: Handoff | None = None
        if step.requires_review:
            self._set_status(task, "tasks", TaskStatus.ACCEPTED)
            self._set_status(step_run, "step_runs", StepStatus.WAITING_REVIEW)
            review = Review(
                id=str(uuid.uuid4()),
                task_id=task.id,
                artifact_id=artifact.id,
                reviewer_role_id=step.role_id,
            )
            self.store.save_entity("reviews", review)
            self._emit(
                step_run.sop_run_id,
                "review_requested",
                {"review_id": review.id, "task_id": task.id},
            )
        else:
            self._set_status(task, "tasks", TaskStatus.ACCEPTED)
            self._set_status(step_run, "step_runs", StepStatus.COMPLETED)
            self._emit(step_run.sop_run_id, "task_accepted", {"task_id": task.id})
            handoff = self._complete_step_and_create_handoff(step_run, task, step)
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
        self._set_status(handoff, "handoffs", HandoffStatus.ACCEPTED)
        handoff.accepted_at = __import__("datetime").datetime.now(
            __import__("datetime").UTC
        )
        self.store.save_entity("handoffs", handoff)
        task = self._get_model("tasks", handoff.from_task_id, Task)
        source_step = self._get_model("step_runs", task.step_run_id, StepRun)
        target = self._find_target_step_run(source_step.sop_run_id, handoff.to_step_id)
        if not self._dependencies_satisfied(target):
            raise WorkflowEngineError(
                "handoff accepted before all dependencies were satisfied"
            )
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

    def approve_review(self, review_id: str) -> StepRun:
        review = self._get_model("reviews", review_id, Review)
        if review.status is not ReviewStatus.PENDING:
            raise WorkflowEngineError("review is already decided")
        self._set_status(review, "reviews", ReviewStatus.APPROVED)
        self.store.save_entity("reviews", review)
        task = self._get_model("tasks", review.task_id, Task)
        step_run = self._get_model("step_runs", task.step_run_id, StepRun)
        step = self.step_definition(step_run.sop_run_id, step_run.step_id)
        self._set_status(task, "tasks", TaskStatus.ACCEPTED)
        self._set_status(step_run, "step_runs", StepStatus.COMPLETED)
        self.store.save_entity("tasks", task)
        self.store.save_entity("step_runs", step_run)
        self._complete_step_and_create_handoff(step_run, task, step)
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
        task = self._get_model("tasks", review.task_id, Task)
        step_run = self._get_model("step_runs", task.step_run_id, StepRun)
        self._set_status(task, "tasks", TaskStatus.REWORK)
        self._set_status(
            step_run,
            "step_runs",
            StepStatus.REWORK if hasattr(StepStatus, "REWORK") else StepStatus.READY,
        )
        self.store.save_entity("tasks", task)
        self.store.save_entity("step_runs", step_run)
        self._emit(
            step_run.sop_run_id,
            "review_rejected",
            {"review_id": review.id, "feedback": review.feedback},
        )
        return step_run

    def _extract_review_feedback(self, review: Review) -> str:
        """Extract structured feedback from the review's artifact.

        Returns the blocking items as formatted feedback for the executor.
        Falls back to routing metadata if artifact parsing fails.
        """
        try:
            import json
            artifact = self._get_model("artifacts", review.artifact_id, Artifact)
            if artifact.content:
                review_data = json.loads(artifact.content)
                blocking = review_data.get("blocking", [])
                if blocking:
                    # Format blocking items as actionable feedback
                    items = "\n".join(f"- {item}" for item in blocking)
                    return f"Review feedback - blocking issues:\n{items}"
            # Fallback: use the verdict as minimal feedback
            return review_data.get("result", "REWORK")
        except (KeyError, json.JSONDecodeError, Exception):
            # Safe fallback: return the verdict itself
            return review.feedback or "Review requires changes"

    def decide_review(self, review_id: str, *, verdict: str) -> tuple[StepRun, RouteTarget]:
        """Route a review verdict through the Engine-owned routing table.

        The destination is never chosen by the Reviewer and never chosen by a
        calling script: :data:`role_contract.REVIEW_ROUTING` is the single
        source of truth and only the Engine may resolve it.
        """
        route = resolve_route(verdict, actor=AgentRole.SOP_ENGINE)

        # Extract actual reviewer feedback from the Review artifact (blocking items)
        review = self._get_model("reviews", review_id, Review)
        feedback = self._extract_review_feedback(review)

        if route is RouteTarget.ADVANCE:
            return self.approve_review(review_id), route
        if route is RouteTarget.RERUN_EXECUTE:
            return (
                self.reject_review(review_id, feedback=feedback),
                route,
            )
        if route is RouteTarget.RETURN_TO_PLAN:
            task = self._get_model("tasks", review.task_id, Task)
            step_run = self._get_model("step_runs", task.step_run_id, StepRun)
            self._emit(
                step_run.sop_run_id,
                "plan_invalid",
                {
                    "review_id": review.id,
                    "task_id": task.id,
                    "route": route.value,
                    "verdict": str(verdict),
                },
            )
            return (
                self.reject_review(review_id, feedback=feedback),
                route,
            )
        raise WorkflowEngineError(f"unroutable verdict {verdict!r} -> {route.value}")

    def retry_task(self, task_id: str) -> Task:
        previous = self._get_model("tasks", task_id, Task)
        step_run = self._get_model("step_runs", previous.step_run_id, StepRun)
        if previous.status not in {
            TaskStatus.REJECTED,
            TaskStatus.FAILED,
            TaskStatus.REWORK,
        }:
            raise WorkflowEngineError("only failed or rejected tasks can be retried")
        self._set_status(step_run, "step_runs", StepStatus.READY)
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
        self.store.save_entity("step_runs", step_run)
        self.store.save_entity("tasks", retry)
        self._emit(
            step_run.sop_run_id,
            "task_retry_ready",
            {"task_id": retry.id, "retry_of": previous.id},
        )
        return retry

    def _complete_step_and_create_handoff(
        self, step_run: StepRun, task: Task, step: StepDefinition
    ) -> Handoff | None:
        self._emit(step_run.sop_run_id, "step_completed", {"step_id": step.id})
        if step.handoff_to:
            handoff = Handoff(
                id=str(uuid.uuid4()),
                from_task_id=task.id,
                to_step_id=step.handoff_to,
                artifact_ids=list(task.output_artifact_ids),
                context_package_id=task.context_package_id,
                status=HandoffStatus.READY,
            )
            self._handoffs[handoff.id] = handoff
            self.store.save_entity("handoffs", handoff)
            self._emit(
                step_run.sop_run_id,
                "handoff_created",
                {"handoff_id": handoff.id, "to_step_id": handoff.to_step_id},
            )
            return handoff
        self._unlock_dependents(step_run.sop_run_id, step.id)
        return None

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

    def _dependencies_satisfied(self, target: StepRun) -> bool:
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
                    and item.get("status") == HandoffStatus.ACCEPTED.value
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
        if steps and all(item.status is StepStatus.COMPLETED for item in steps):
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

    def _find_target_step_run(self, sop_run_id: str, step_id: str) -> StepRun:
        for item in self.store.list_entities("step_runs"):
            if item.get("sop_run_id") == sop_run_id and item.get("step_id") == step_id:
                return StepRun.model_validate(item)
        raise WorkflowEngineError(f"unknown step run: {step_id}")

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
