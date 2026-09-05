"""Controlled orchestration helpers for manual and automatic SOP execution."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ..domain.models import (
    ArtifactType,
    ContextPackage,
    HandoffMessageType,
    HandoffStatus,
    Review,
    StepDefinition,
    SubagentAssignment,
    Task,
    TaskStatus,
)
from .engine import (
    ExecutionResult,
    ReviewRouteDecision,
    WorkflowEngine,
    WorkflowEngineError,
)
from .role_contract import RouteTarget

AssignmentResolver = Callable[
    [Task, StepDefinition], SubagentAssignment | Awaitable[SubagentAssignment]
]
ContextBuilder = Callable[
    [Task, StepDefinition], ContextPackage | Awaitable[ContextPackage]
]
ReviewOutcomeResolver = Callable[
    [ExecutionResult], str | Awaitable[str]
]


class ManualOrchestrator:
    """Resolve pre-approved assignments and context packages without auto-routing."""

    def __init__(
        self,
        *,
        goal: Any,
        assignments: Mapping[str, SubagentAssignment],
        context_packages: Mapping[str, ContextPackage],
    ) -> None:
        self.goal = goal
        self.assignments = dict(assignments)
        self.context_packages = dict(context_packages)

    def prepare(self, task: Task) -> tuple[SubagentAssignment, ContextPackage]:
        if task.id in self.assignments and task.id in self.context_packages:
            return self.assignments[task.id], self.context_packages[task.id]

        # A task ID is created by the engine, while a pre-approved manual
        # assignment may have been authored against a stable SOP task key.
        # Rebind only the selected records to the concrete attempt ID; the
        # role and runtime approval remain unchanged.
        candidates = [
            (assignment, self.context_packages[key])
            for key, assignment in self.assignments.items()
            if key in self.context_packages and assignment.role_id == task.role_id
        ]
        if len(candidates) == 1:
            assignment, context = candidates[0]
            return (
                assignment.model_copy(update={"task_id": task.id}),
                context.model_copy(update={"task_id": task.id}),
            )
        raise ValueError(f"no approved assignment/context for task {task.id}")


class AutoOrchestrator:
    """Automatically advance ready steps until a human gate or terminal state."""

    def __init__(
        self,
        engine: WorkflowEngine,
        *,
        review_outcome_resolver: ReviewOutcomeResolver | None = None,
        max_cycles: int = 100,
        manual_handoffs: bool = False,
    ) -> None:
        if max_cycles < 1:
            raise ValueError("max_cycles must be at least 1")
        self.engine = engine
        self.review_outcome_resolver = review_outcome_resolver
        self.max_cycles = max_cycles
        self.manual_handoffs = manual_handoffs
        self.review_decisions: list[ReviewRouteDecision] = []
        self._correlations: dict[str, str] = {}

    async def run_until_gate(
        self,
        sop_run_id: str,
        *,
        resolve_assignment: AssignmentResolver,
        build_context: ContextBuilder,
    ) -> list[ExecutionResult]:
        results: list[ExecutionResult] = []
        cycles = 0
        while True:
            cycles += 1
            if cycles > self.max_cycles:
                raise WorkflowEngineError(
                    f"orchestration cycle limit exceeded for SOP run {sop_run_id}"
                )

            if not self.manual_handoffs:
                self._accept_route_handoffs(sop_run_id)
            ready = self.engine.ready_step_runs(sop_run_id)
            if not ready:
                break

            ready_steps = [
                (step_run, self.engine.step_definition(sop_run_id, step_run.step_id))
                for step_run in ready
            ]
            first_step = ready_steps[0][1]
            selected = (
                ready_steps
                if first_step.execution_mode.value == "parallel"
                else ready_steps[:1]
            )
            batch: list[tuple[Task, StepDefinition]] = []
            for step_run, step in selected:
                task = self.engine.create_task(step_run.id, role_id=step.role_id)
                batch.append((task, step))
            batch_results = await asyncio.gather(
                *(
                    self._execute(task, step, resolve_assignment, build_context)
                    for task, step in batch
                )
            )
            results.extend(batch_results)

            if any(
                result.task.status is not TaskStatus.ACCEPTED
                for result in batch_results
            ):
                break

            for result, (_, step) in zip(batch_results, batch, strict=True):
                if result.artifact.type is ArtifactType.REVIEW_REPORT:
                    decision = await self._apply_review_result(result)
                    self.review_decisions.append(decision)
                    if decision.route is RouteTarget.FAIL_RUN:
                        return results
                    if decision.route is RouteTarget.ADVANCE:
                        self._accept_handoffs_for_task(result.task.id)
                    continue

                if not step.requires_review and not self.manual_handoffs:
                    self._accept_handoffs_for_task(result.task.id)
        return results

    def _accept_route_handoffs(self, sop_run_id: str) -> None:
        """Consume Engine-authored rework handoffs before creating a task."""
        route_types = {
            HandoffMessageType.REWORK.value,
            HandoffMessageType.PLAN_INVALID.value,
        }
        try:
            handoffs = self.engine.store.list_entities("handoffs")
        except (KeyError, AttributeError):
            return
        for item in list(handoffs):
            if item.get("status") != HandoffStatus.READY.value:
                continue
            if item.get("message_type") not in route_types:
                continue
            try:
                source_task = self.engine.store.get_entity(
                    "tasks", item["from_task_id"]
                )
                source_step = self.engine.store.get_entity(
                    "step_runs", source_task["step_run_id"]
                )
            except (KeyError, TypeError):
                continue
            if source_step.get("sop_run_id") != sop_run_id:
                continue
            self.engine.accept_handoff(item["id"])

    def _accept_handoffs_for_task(self, task_id: str) -> None:
        try:
            handoffs = self.engine.store.list_entities("handoffs")
        except (KeyError, AttributeError):
            return
        for item in list(handoffs):
            if item.get("from_task_id") == task_id and item.get(
                "status"
            ) == HandoffStatus.READY.value:
                self.engine.accept_handoff(item["id"])

    def _review_for_result(self, result: ExecutionResult) -> Review:
        try:
            reviews = self.engine.store.list_entities("reviews")
        except (KeyError, AttributeError):
            reviews = []
        for item in reviews:
            if item.get("artifact_id") == result.artifact.id:
                return self.engine._get_model("reviews", item["id"], Review)
        return self.engine.ensure_review_for_report(
            task=result.task,
            artifact=result.artifact,
            context=self.engine._get_model(
                "contexts", result.task.context_package_id, ContextPackage
            )
            if result.task.context_package_id
            else ContextPackage(
                id=f"review-context-{result.task.id}",
                task_id=result.task.id,
                goal_summary=result.task.description,
                instructions=result.task.description,
            ),
        )

    async def _apply_review_result(
        self, result: ExecutionResult
    ) -> ReviewRouteDecision:
        review = self._review_for_result(result)
        if self.review_outcome_resolver is None:
            from ..agents.codex_review import (
                ReviewParseError,
                ReviewValidationError,
                parse_review,
            )

            try:
                parsed = parse_review(result.artifact.content or "")
            except (ReviewParseError, ReviewValidationError) as exc:
                raise WorkflowEngineError(
                    f"unable to parse review artifact {result.artifact.id}: {exc}"
                ) from exc
            verdict = parsed.result
        else:
            verdict = self.review_outcome_resolver(result)
            if asyncio.iscoroutine(verdict):
                verdict = await verdict
        if not isinstance(verdict, str):
            raise TypeError("review outcome resolver returned an invalid value")
        step_run = self.engine.store.get_entity("step_runs", result.task.step_run_id)
        run_id = step_run["sop_run_id"]
        correlation_id = self._correlations.setdefault(run_id, str(uuid.uuid4()))
        return self.engine.apply_review_outcome(
            review.id,
            verdict=verdict,
            correlation_id=correlation_id,
        )

    async def apply_review_result(
        self, result: ExecutionResult
    ) -> ReviewRouteDecision:
        """Apply one reviewer artifact after an explicit manual dispatch.

        The Engine remains the sole owner of the review route and all SOP
        status changes; this public wrapper only makes the existing routing
        path available to the host's manual Handoff dispatcher.
        """
        decision = await self._apply_review_result(result)
        self.review_decisions.append(decision)
        return decision

    async def _execute(
        self,
        task: Task,
        step: StepDefinition,
        resolve_assignment: AssignmentResolver,
        build_context: ContextBuilder,
    ) -> ExecutionResult:
        assignment = resolve_assignment(task, step)
        if asyncio.iscoroutine(assignment):
            assignment = await assignment
        context = build_context(task, step)
        if asyncio.iscoroutine(context):
            context = await context
        if not isinstance(assignment, SubagentAssignment):
            raise TypeError("assignment resolver returned an invalid value")
        if not isinstance(context, ContextPackage):
            raise TypeError("context builder returned an invalid value")
        context = self._augment_context_from_handoffs(task, step, context)
        return await self.engine.execute_task(
            task.id,
            assignment=assignment,
            context=context,
        )

    def _augment_context_from_handoffs(
        self, task: Task, step: StepDefinition, context: ContextPackage
    ) -> ContextPackage:
        """Ensure Engine route feedback reaches the next attempt.

        A custom context builder may intentionally omit upstream handoff ids;
        the Engine-authored route message is still authoritative and must be
        visible to the executor/planner.
        """
        try:
            step_run = self.engine.store.get_entity("step_runs", task.step_run_id)
            run_id = step_run["sop_run_id"]
        except (KeyError, TypeError):
            return context
        artifact_ids = list(context.artifact_ids)
        instructions = context.instructions
        try:
            handoffs = self.engine.store.list_entities("handoffs")
        except (KeyError, AttributeError):
            handoffs = []
        for item in handoffs:
            if item.get("to_step_id") != step.id or item.get("status") != "accepted":
                continue
            try:
                source_task = self.engine.store.get_entity("tasks", item["from_task_id"])
                source_step = self.engine.store.get_entity(
                    "step_runs", source_task["step_run_id"]
                )
            except (KeyError, TypeError):
                continue
            if source_step.get("sop_run_id") != run_id:
                continue
            for artifact_id in item.get("artifact_ids", []):
                if artifact_id not in artifact_ids:
                    artifact_ids.append(artifact_id)
            brief = item.get("brief", "")
            if brief and brief not in instructions:
                instructions = f"{instructions}\n\n{brief}".strip()
        if artifact_ids == context.artifact_ids and instructions == context.instructions:
            return context
        return context.model_copy(
            update={"artifact_ids": artifact_ids, "instructions": instructions}
        )


SOPOrchestrator = AutoOrchestrator
