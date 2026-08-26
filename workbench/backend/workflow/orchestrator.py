"""Controlled orchestration helpers for manual and automatic SOP execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ..domain.models import (
    ContextPackage,
    StepDefinition,
    SubagentAssignment,
    Task,
    TaskStatus,
)
from .engine import ExecutionResult, WorkflowEngine

AssignmentResolver = Callable[
    [Task, StepDefinition], SubagentAssignment | Awaitable[SubagentAssignment]
]
ContextBuilder = Callable[
    [Task, StepDefinition], ContextPackage | Awaitable[ContextPackage]
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

    def __init__(self, engine: WorkflowEngine) -> None:
        self.engine = engine

    async def run_until_gate(
        self,
        sop_run_id: str,
        *,
        resolve_assignment: AssignmentResolver,
        build_context: ContextBuilder,
    ) -> list[ExecutionResult]:
        results: list[ExecutionResult] = []
        while True:
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
                handoffs = [
                    item
                    for item in self.engine.store.list_entities("handoffs")
                    if item.get("from_task_id") == result.task.id
                    and item.get("status") == "ready"
                ]
                if not step.requires_review:
                    for handoff in handoffs:
                        self.engine.accept_handoff(handoff["id"])
        return results

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
        return await self.engine.execute_task(
            task.id,
            assignment=assignment,
            context=context,
        )


SOPOrchestrator = AutoOrchestrator
